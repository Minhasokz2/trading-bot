"""Hosted web app (v6.1+): run audits from a browser, watch them, read the reports and the charts. Password protected.

    python audit/serve.py          # Render / Docker start this; it also works on your own machine

Design
  * The server only orchestrates: every audit runs as a subprocess (see jobs.py), so the web process stays
    small and an out-of-memory kill in a job cannot take the site down.
  * One middleware guards every route except /healthz and the two static scripts: HTTP Basic authentication
    with a constant-time comparison, a failed-login throttle, a same-origin check on every POST (Basic
    credentials are sent by browsers automatically, so cross-site form posts must be refused) and strict
    security headers. Pages allow no inline script: the two scripts are files, plus one hashed theme snippet.
  * Nothing here can place an order; there is no API key anywhere in the project.

Environment (all optional except the password)
  COIN_AUDIT_PASSWORD        the login password (Render generates one). Without it every page returns 503.
  COIN_AUDIT_USER            login name (default "admin")
  COIN_AUDIT_DATA_DIR        where reports, logs and caches live (the persistent disk, e.g. /data)
  COIN_AUDIT_ALLOW_ANONYMOUS 1 = no login at all (local experiments only)
  COIN_AUDIT_TRUST_PROXY     1 = take the client address from True-Client-IP / X-Forwarded-For (behind Render's proxy)
  COIN_AUDIT_CLIENT_IP_HEADER  header that carries the real client address (default true-client-ip)
  COIN_AUDIT_MAX_JOBS        parallel jobs (default 1: one audit needs about 500 MB of memory)
  COIN_AUDIT_KEEP_DAYS       report retention in days (default 14; the newest report per coin is always kept)
  COIN_AUDIT_WATCHLIST etc.  scheduled runs, see jobs.schedules_from_env
"""
from __future__ import annotations

import base64
import csv
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import markdown as md_lib
import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

import chart
import dashboard
import jobs
from settings import DATA_DIR, __version__

AUDIT_PY = Path(__file__).resolve().with_name("audit.py")
STATIC = Path(__file__).resolve().with_name("static")
OPEN_PATHS = {"/healthz", "/static/app.js", "/static/chart.js"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
REPORT_NAME_RE = re.compile(r"^([A-Z0-9]+)_(15m|1h|4h|1d)_(\d{4}-\d{2}-\d{2})_(\d{4})(?:_r\d+)?$")
SCAN_NAME_RE = re.compile(r"^scan_(15m|1h|4h|1d)_(\d{4}-\d{2}-\d{2})_(\d{4})(?:_r\d+)?$")
# The theme is applied before the page paints by one tiny inline script; its hash is the only inline script the CSP allows.
THEME_JS = ("(function(){try{var t=localStorage.getItem('coin-audit-theme');"
            "if(t==='dark'||t==='light'){document.documentElement.setAttribute('data-theme',t)}}catch(e){}})();")
THEME_JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(THEME_JS.encode("utf-8")).digest()).decode("ascii")
STRICT_CSP = (f"default-src 'none'; style-src 'unsafe-inline'; script-src 'self' '{THEME_JS_HASH}'; connect-src 'self'; "
              "img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
SANDBOX_CSP = "sandbox allow-scripts; default-src 'self' 'unsafe-inline' 'unsafe-eval' data: https:"
NO_PASSWORD = ("Coin Audit is running but has no login password, so every page is locked.\n\n"
               "Set the COIN_AUDIT_PASSWORD environment variable (on Render: Environment tab), then redeploy.\n"
               "For a purely local experiment you can set COIN_AUDIT_ALLOW_ANONYMOUS=1 instead.\n")

CHECKS = [
    ("Binance Spot market data", "https://data-api.binance.vision/api/v3/ping", "required: all candles and order books"),
    ("Binance Spot API", "https://api.binance.com/api/v3/ping", "not used by the audit; shows how strict the region is"),
    ("Binance USD-M futures", "https://fapi.binance.com/fapi/v1/ping", "optional: funding, open interest, long/short"),
    ("CoinGecko", "https://api.coingecko.com/api/v3/ping", "optional: market caps for the TOTAL/dominance layer"),
    ("DefiLlama stablecoins", "https://stablecoins.llama.fi/stablecoins?includePrices=false", "optional: stablecoin supply"),
    ("FRED (St. Louis Fed)", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS", "optional: macro series"),
]


def _bool(v, default=False) -> bool:
    return default if v is None else str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class WebConfig:
    data_dir: Path = DATA_DIR
    user: str = "admin"
    password: str = ""
    allow_anonymous: bool = False
    trust_proxy: bool = False
    client_ip_header: str = "true-client-ip"
    max_jobs: int = 1
    max_queue: int = 20
    keep_days: float = 14
    allowed_origins: set = field(default_factory=set)

    @classmethod
    def from_env(cls, env=None) -> "WebConfig":
        env = os.environ if env is None else env
        return cls(data_dir=Path(env.get("COIN_AUDIT_DATA_DIR") or DATA_DIR), user=env.get("COIN_AUDIT_USER") or "admin",
                   password=env.get("COIN_AUDIT_PASSWORD") or "", allow_anonymous=_bool(env.get("COIN_AUDIT_ALLOW_ANONYMOUS")),
                   trust_proxy=_bool(env.get("COIN_AUDIT_TRUST_PROXY")),
                   client_ip_header=(env.get("COIN_AUDIT_CLIENT_IP_HEADER") or "true-client-ip").lower(),
                   max_jobs=int(env.get("COIN_AUDIT_MAX_JOBS") or 1),
                   keep_days=float(env.get("COIN_AUDIT_KEEP_DAYS") or 14),
                   allowed_origins={o.strip() for o in (env.get("COIN_AUDIT_ALLOWED_ORIGINS") or "").split(",") if o.strip()})

    @property
    def reports(self) -> Path:
        return self.data_dir / "reports"

    @property
    def audits_csv(self) -> Path:
        return self.data_dir / "logs" / "audits.csv"

    @property
    def signals_csv(self) -> Path:
        return self.data_dir / "logs" / "signals.csv"


class Throttle:
    """Refuse an address for a while after too many wrong passwords (best effort, in memory).
    Only addresses that actually failed are tracked, and the table is capped."""

    def __init__(self, limit: int = 8, window: float = 600.0, clock=time.time, max_tracked: int = 5000):
        self.limit, self.window, self.clock, self.max_tracked, self.fails = limit, window, clock, max_tracked, {}

    def _recent(self, ip: str) -> list:
        now = self.clock()
        recent = [t for t in self.fails.get(ip, []) if now - t < self.window]
        if recent:
            self.fails[ip] = recent
        else:
            self.fails.pop(ip, None)
        return recent

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.limit

    def fail(self, ip: str) -> None:
        recent = self._recent(ip)
        recent.append(self.clock())
        self.fails[ip] = recent
        if len(self.fails) > self.max_tracked:                    # drop the oldest-failing addresses
            for old in sorted(self.fails, key=lambda k: self.fails[k][-1])[: len(self.fails) - self.max_tracked]:
                self.fails.pop(old, None)

    def ok(self, ip: str) -> None:
        self.fails.pop(ip, None)


def client_ip(request: Request, trust_proxy: bool, header: str = "true-client-ip") -> str:
    """The address the failed-login throttle is keyed on. Behind Render's edge (Cloudflare) the socket address and the
    end of X-Forwarded-For are shared proxy addresses, and the start of X-Forwarded-For is client-controlled; Render puts
    the real client in True-Client-IP. Order: that header, then the last X-Forwarded-For entry, then the socket."""
    if trust_proxy:
        real = (request.headers.get(header) or "").strip()
        if real:
            return real
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def basic_credentials(request: Request):
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("basic "):
        return None
    try:
        user, _, pw = base64.b64decode(h[6:].strip(), validate=True).decode("utf-8").partition(":")
        return user, pw
    except (ValueError, UnicodeDecodeError):
        return ("", "")


def credentials_ok(cred, cfg: WebConfig) -> bool:
    if cred is None:
        return False
    u_ok = secrets.compare_digest(cred[0].encode(), cfg.user.encode())
    p_ok = secrets.compare_digest(cred[1].encode(), cfg.password.encode())
    return u_ok and p_ok


def same_origin(request: Request, cfg: WebConfig) -> bool:
    """Is this POST from our own pages? Browsers send Sec-Fetch-Site and it cannot be forged by a web page, so it decides
    when present. Older browsers fall back to the Origin header. (Never rely on Origin alone: a page served with
    'Referrer-Policy: no-referrer' makes browsers send 'Origin: null' on its own form posts.)"""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site.lower() in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin:
        return True                                        # curl / scripts; browsers always send Origin or Sec-Fetch-Site
    if origin == "null":
        return False
    hosts = {request.headers.get("host", "").lower(), request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower(),
             os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").lower()}
    return urlparse(origin).netloc.lower() in (hosts - {""}) or origin in cfg.allowed_origins


def secure(resp: Response, csp: str = STRICT_CSP) -> Response:
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")      # NOT no-referrer: that makes browsers send Origin: null on forms
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers.setdefault("Content-Security-Policy", csp)
    return resp


def check_connectivity(http_get=requests.get, timeout: float = 8.0) -> list[dict]:
    """Can THIS server reach the data sources? Binance blocks some regions (HTTP 451), so this is the
    first thing to look at after a deploy."""
    def one(item):
        name, url, purpose = item
        t0 = time.time()
        try:
            r = http_get(url, timeout=timeout, stream=True, headers={"User-Agent": "coin-audit-bot/" + __version__})
            code = r.status_code
            r.close()
            ms = round((time.time() - t0) * 1000)
            if code == 200:
                verdict = "ok"
            elif code == 451:
                verdict = "BLOCKED — HTTP 451: this server's region is restricted by the provider"
            elif code in (401, 403):
                verdict = f"BLOCKED — HTTP {code}: refused (region or network policy)"
            elif code == 429:
                verdict = "rate limited — retry in a minute"
            else:
                verdict = f"unexpected HTTP {code}"
            return {"name": name, "url": url, "purpose": purpose, "status": code, "ms": ms, "verdict": verdict, "ok": code == 200}
        except requests.RequestException as e:
            return {"name": name, "url": url, "purpose": purpose, "status": None, "ms": None, "ok": False,
                    "verdict": f"unreachable — {type(e).__name__}: {str(e)[:90]}"}
    with ThreadPoolExecutor(max_workers=len(CHECKS)) as ex:
        return list(ex.map(one, CHECKS))


# ------------------------------------------------------------------- HTML
E = html.escape
APP_CSS = """
body { padding:0; }
.top { position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:6px 12px; flex-wrap:wrap; padding:9px 18px;
  background:var(--surface); border-bottom:1px solid var(--border); }
.brand { font-weight:700; font-size:16px; color:var(--ink); display:inline-flex; align-items:center; gap:8px; margin-right:6px; }
.brand:hover { text-decoration:none; }
.brand .logo { width:22px; height:22px; border-radius:6px; background:linear-gradient(135deg, var(--accent), var(--good)); display:inline-block; }
.top nav { display:flex; gap:2px; flex-wrap:wrap; }
.top nav a { padding:6px 10px; border-radius:6px; color:var(--ink2); font-weight:600; font-size:13px; }
.top nav a:hover { color:var(--ink); text-decoration:none; background:var(--plane); }
.top nav a[aria-current=page] { color:var(--ink); background:var(--plane); box-shadow:inset 0 -2px 0 var(--accent); }
.top .spacer { flex:1; } .top .ver { color:var(--muted); font-size:12px; }
.theme { background:transparent; border:1px solid var(--border); color:var(--ink2); border-radius:6px; padding:4px 9px; font:inherit; font-size:12px; cursor:pointer; margin:0; }
.page { max-width:1240px; margin:0 auto; padding:20px 16px 40px; }
.page > h1 { font-size:22px; margin:4px 0 4px; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:16px; align-items:start; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px 16px 16px; margin:0; }
.card h2 { margin:0 0 6px; font-size:15px; } .card .hint { color:var(--ink2); font-size:12px; margin:0 0 6px; }
label { display:block; font-size:12px; color:var(--ink2); margin:10px 0 3px; }
input[type=text], input[type=number], select { width:100%; padding:8px 10px; border:1px solid var(--border); border-radius:6px;
  background:var(--plane); color:var(--ink); font:inherit; }
input.big { font-size:17px; font-weight:600; text-transform:uppercase; letter-spacing:.03em; }
.row { display:grid; grid-template-columns:minmax(0, 2fr) minmax(0, 1fr); gap:12px; align-items:end; }
.row3 { grid-template-columns:repeat(3, minmax(0, 1fr)); }
details.adv { margin-top:10px; } details.adv summary { cursor:pointer; color:var(--ink2); font-size:13px; }
.checks { display:flex; flex-wrap:wrap; gap:4px 16px; margin-top:4px; }
.checks label { display:inline-flex; align-items:center; gap:6px; margin:6px 0 0; color:var(--ink); font-size:13px; }
button, .btn { background:var(--accent); color:#fff; border:0; border-radius:6px; padding:9px 16px; font:inherit; font-weight:600;
  cursor:pointer; margin-top:12px; display:inline-block; text-decoration:none; line-height:1.2; }
button:hover, .btn:hover { filter:brightness(1.08); text-decoration:none; }
button.secondary, .btn.secondary { background:transparent; color:var(--ink); border:1px solid var(--border); margin-top:0; }
button.small, .btn.small { padding:5px 10px; font-size:12px; margin-top:0; }
.badge { display:inline-flex; align-items:center; gap:6px; padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600;
  border:1px solid var(--border); white-space:nowrap; color:var(--ink2); }
.badge.done { color:var(--good); border-color:var(--good); } .badge.failed, .badge.timeout { color:var(--critical); border-color:var(--critical); }
.badge.running { color:var(--accent); border-color:var(--accent); } .badge.queued { color:var(--warning); }
.spin { width:10px; height:10px; border:2px solid var(--accent); border-right-color:transparent; border-radius:50%; display:inline-block;
  animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
pre.log { background:var(--plane); border:1px solid var(--border); border-radius:8px; padding:12px; overflow:auto; max-height:55vh;
  font-size:12px; white-space:pre-wrap; word-break:break-word; margin:0; }
.banner { border:1px solid var(--border); border-left:4px solid var(--warning); background:var(--surface); padding:10px 14px; border-radius:6px; margin:10px 0; }
.banner.err { border-left-color:var(--critical); } .banner.ok { border-left-color:var(--good); }
.actions { display:flex; gap:10px; flex-wrap:wrap; align-items:center; } .actions form { margin:0; }
.md { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:8px 20px 20px; overflow-x:auto; }
.md table { border-collapse:collapse; margin:10px 0; min-width:0; } .md th, .md td { border:1px solid var(--grid); padding:4px 8px; font-size:13px; }
.md code { font-size:12px; } .md pre { overflow:auto; background:var(--plane); padding:10px; border-radius:6px; }
.md blockquote { margin:6px 0; padding:2px 12px; border-left:3px solid var(--warning); color:var(--ink2); }
.md h1 { font-size:20px; } .md h2 { font-size:16px; margin-top:26px; } .md h3 { font-size:14px; margin:18px 0 6px; }
.cards { display:grid; grid-template-columns:repeat(auto-fill, minmax(250px, 1fr)); gap:12px; }
.acard { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px 14px; display:flex; flex-direction:column; gap:6px; }
.acard .sym { font-size:17px; font-weight:700; } .acard .sym .tf { color:var(--ink2); font-weight:500; font-size:13px; margin-left:6px; }
.acard .when, .rel { color:var(--muted); font-size:12px; }
.acard .plan { font-size:12px; color:var(--ink2); font-variant-numeric:tabular-nums; }
.acard .links { display:flex; gap:12px; margin-top:2px; font-size:13px; font-weight:600; }
.score { display:inline-flex; align-items:center; gap:8px; font-variant-numeric:tabular-nums; font-weight:600; }
.summary { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px 16px; margin:12px 0 4px; }
.summary .k { color:var(--ink2); font-size:12px; } .summary .v { font-size:15px; font-weight:600; font-variant-numeric:tabular-nums; }
.summary .v.up { color:var(--good); } .summary .v.down { color:var(--critical); } .summary .meter { width:64px; }
.stage { color:var(--ink2); font-size:13px; margin:6px 0; }
.filter { max-width:320px; margin:0 0 10px; }
.wrap table { min-width:640px; } .wrap.tight table { min-width:0; } .tile .delta { overflow-wrap:anywhere; }
.muted { color:var(--ink2); } .ok { color:var(--good); } .bad { color:var(--critical); }
.jobs td:first-child { max-width:380px; }
@media (max-width:640px) { .row, .row3 { grid-template-columns:1fr; } .page { padding:14px 12px 32px; } .top { padding:8px 12px; } }
"""
STATUS_LABEL = {"queued": "⏳ queued", "running": "▶ running", "done": "✔ done", "failed": "✖ failed",
                "cancelled": "⊘ cancelled", "timeout": "⏱ timed out"}
NAV_ITEMS = [("home", "/", "Home"), ("dashboard", "/dashboard", "Dashboard"), ("reports", "/reports", "Reports"),
             ("jobs", "/jobs", "Jobs"), ("connectivity", "/connectivity", "Connectivity")]
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
CURRENT = ' aria-current="page"'


def page(title: str, body: str, csp: str = STRICT_CSP, active: str = "") -> HTMLResponse:
    nav = "".join(f'<a href="{href}"{CURRENT if key == active else ""}>{label}</a>' for key, href, label in NAV_ITEMS)
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<title>{E(title)} · Coin Audit</title><script>{THEME_JS}</script><style>{dashboard.CSS}{chart.CSS}{APP_CSS}</style></head><body>'
           f'<header class="top"><a class="brand" href="/"><span class="logo"></span>Coin Audit</a><nav>{nav}</nav><span class="spacer"></span>'
           f'<button class="theme" id="theme" type="button" title="switch light / dark">◐ theme</button><span class="ver">v{E(__version__)}</span></header>'
           f'<main class="page">{body}</main>'
           f'<p class="foot page">Coin Audit {E(__version__)} · read-only Binance Spot research — no orders, no API keys · not financial advice</p>'
           f'<script src="/static/app.js"></script></body></html>')
    return secure(HTMLResponse(doc), csp)


def badge(status: str) -> str:
    spin = '<span class="spin"></span>' if status == "running" else ""
    return f'<span class="badge {E(status)}">{spin}{E(STATUS_LABEL.get(status, status))}</span>'


def verdict_badge(verdict: str, score=None) -> str:
    cls, icon = dashboard.VERDICT.get(verdict or "", ("muted", "○"))
    s = "" if score in (None, "") else f' <span class="score"><span class="meter" title="score {E(str(score))}/100"><i style="width:{_pct(score)}%"></i></span>{_num(score)}</span>'
    return f'<span class="v {cls}"><span class="dot"></span>{icon} {E(verdict or "—")}</span>{s}'


def _pct(x) -> int:
    try:
        return max(0, min(100, int(float(x))))
    except (TypeError, ValueError):
        return 0


def _num(x) -> str:
    try:
        return f"{float(x):.0f}"
    except (TypeError, ValueError):
        return "—"


def _px(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.2f}" if v >= 100 else f"{v:,.4f}" if v >= 1 else f"{v:.8f}".rstrip("0")


def rel_time(iso: str) -> str:
    """'3 min ago' for the job list; the exact UTC time stays in the title."""
    try:
        t = datetime.strptime(iso[:19], "%Y-%m-%d %H:%M:%S" if len(iso) > 16 else "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    s = (datetime.now(timezone.utc) - t).total_seconds()
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s / 60:.0f} min ago"
    if s < 48 * 3600:
        return f"{s / 3600:.0f} h ago"
    return f"{s / 86400:.0f} days ago"


def duration(job: dict) -> str:
    try:
        a = datetime.strptime(job.get("started") or "", "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(job.get("finished") or "", "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""
    s = (b - a).total_seconds()
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min"


def jobs_table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="muted">No jobs yet.</p>'
    body = "".join(
        f'<tr><td><a href="/jobs/{E(j["id"])}">{E(j["label"])}</a></td><td>{badge(j["status"])}</td>'
        f'<td title="{E(j["created"])} UTC">{E(j["created"][:16])} <span class="rel">{E(rel_time(j["created"]))}</span></td>'
        f'<td class="num">{E(duration(j))}</td><td class="num">{len(j.get("reports") or [])}</td><td class="muted">{E(j["source"])}</td></tr>'
        for j in rows)
    return (f'<div class="wrap"><table class="jobs"><thead><tr><th>Job</th><th>Status</th><th>Created (UTC)</th><th>Took</th>'
            f'<th>Reports</th><th>Started by</th></tr></thead><tbody>{body}</tbody></table></div>')


def checkbox(name: str, label: str, checked: bool) -> str:
    return f'<label><input type="checkbox" name="{name}"{" checked" if checked else ""}> {E(label)}</label>'


def tf_select(name: str, include_all: bool, default: str = "4h") -> str:
    opts = list(jobs.TIMEFRAMES) + (["all"] if include_all else [])
    return f'<select name="{name}">' + "".join(f'<option{" selected" if o == default else ""}>{o}</option>' for o in opts) + "</select>"


# ------------------------------------------------------------ report catalogue
def audit_log(path: Path) -> dict:
    """audits.csv rows keyed by the report stem (SYMBOL_tf_YYYY-MM-DD_HHMM): verdict, score, plan, approved signals."""
    out: dict = {}
    if not path.exists():
        return out
    try:
        for r in csv.DictReader(path.open(encoding="utf-8")):
            stamp = (r.get("audit_time_utc") or "").replace(" ", "_").replace(":", "")
            if r.get("symbol") and r.get("timeframe") and stamp:
                out[f"{r['symbol']}_{r['timeframe']}_{stamp}"] = r
    except (OSError, csv.Error):
        pass
    return out


def report_rows(cfg: WebConfig) -> list[dict]:
    """One row per audit or scan on disk (newest first), joined with audits.csv — no JSON is opened."""
    if not cfg.reports.exists():
        return []
    files = {p.name: p for p in cfg.reports.iterdir() if p.is_file() and NAME_RE.match(p.name)}
    log = audit_log(cfg.audits_csv)
    rows = []
    for name, p in files.items():
        if not name.endswith(".md") or name == "dashboard.md":
            continue
        base = name[:-3]
        row = {"base": base, "md": name, "json": f"{base}.json" in files, "chart": f"{base}_chart.html" in files,
               "tearsheet": f"{base}_tearsheet.html" in files, "mtime": p.stat().st_mtime,
               "modified": time.strftime("%Y-%m-%d %H:%M", time.gmtime(p.stat().st_mtime)), "kind": "file",
               "symbol": "", "tf": "", "when": "", "verdict": "", "score": "", "approved": "", "log": None}
        m = REPORT_NAME_RE.match(base)
        s = SCAN_NAME_RE.match(base)
        if m:
            row.update(kind="audit", symbol=m.group(1), tf=m.group(2), when=f"{m.group(3)} {m.group(4)[:2]}:{m.group(4)[2:]}")
            lr = log.get(base) or log.get(re.sub(r"_r\d+$", "", base))
            if lr:
                row.update(verdict=lr.get("verdict", ""), score=lr.get("score", ""), approved=lr.get("approved_signals", ""), log=lr)
        elif s:
            row.update(kind="scan", symbol="Market scan", tf=s.group(1), when=f"{s.group(2)} {s.group(3)[:2]}:{s.group(3)[2:]}")
        rows.append(row)
    rows.sort(key=lambda r: (r["when"] or "", r["mtime"]), reverse=True)
    return rows


def latest_per_coin(rows: list[dict], limit: int = 8) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        if r["kind"] != "audit" or (r["symbol"], r["tf"]) in seen:
            continue
        seen.add((r["symbol"], r["tf"]))
        out.append(r)
        if len(out) >= limit:
            break
    return out


def audit_card(r: dict) -> str:
    lr = r.get("log") or {}
    plan = (f'entry {_px(lr.get("entry_low"))}–{_px(lr.get("entry_high"))} · stop {_px(lr.get("stop"))} · T1 {_px(lr.get("target1"))}'
            if lr else "")
    appr = f'<span class="ok">{E(str(r["approved"]))} approved signal{"s" if str(r["approved"]) != "1" else ""}</span>' if str(r.get("approved") or "0") not in ("0", "") else '<span class="muted">no approved signal</span>'
    links = f'<a href="/reports/{E(r["md"])}">Report</a>' + (f'<a href="/reports/{E(r["base"])}_chart.html">Chart</a>' if r["chart"] else "")
    return (f'<div class="acard"><div class="sym">{E(r["symbol"])}<span class="tf">{E(r["tf"])}</span></div>'
            f'<div>{verdict_badge(r["verdict"], r["score"])}</div><div class="when">{E(r["when"])} UTC · {E(rel_time(r["when"]))}</div>'
            f'<div class="plan">{E(plan)}</div><div>{appr}</div><div class="links">{links}</div></div>')


# ----------------------------------------------------------------------- pages
def audit_form(coins: str = "", tf: str = "4h", compact: bool = False) -> str:
    adv = (f'<details class="adv"><summary>Options</summary><div class="checks">{checkbox("ml", "ML meta-labeler", True)}'
           f'{checkbox("market", "Crypto-wide + macro layer", True)}{checkbox("futures", "Futures data (funding, OI)", True)}'
           f'{checkbox("tearsheet", "HTML tearsheet (slow)", False)}{checkbox("notify", "Send alert", False)}</div>'
           f'<label>Only these strategies (optional: ids, families or name fragments, comma separated)</label>'
           f'<input type="text" name="strategies" maxlength="200" placeholder="all 27"></details>')
    return (f'<form class="card" method="post" action="/jobs"><input type="hidden" name="kind" value="audit">'
            f'<h2>Audit a coin</h2><p class="hint">Validated strategies, chart patterns, market regime, sizing — the full report with the chart. About a minute per coin.</p>'
            f'<div class="row"><div><label>Coin(s), e.g. SOL or SOL ARB INJ</label><input class="big" type="text" name="coins" value="{E(coins)}" '
            f'placeholder="SOL" required maxlength="120" autocomplete="off" autocapitalize="characters"></div>'
            f'<div><label>Timeframe</label>{tf_select("tf", True, tf)}</div></div>{"" if compact else adv}'
            f'<button>Run audit</button></form>')


def scan_form() -> str:
    return (f'<form class="card" method="post" action="/jobs"><input type="hidden" name="kind" value="scan"><h2>Scan the market</h2>'
            f'<p class="hint">Audit the most traded coins, rank them and get a correlation-aware allocation. Several minutes per coin.</p>'
            f'<div class="row row3"><div><label>Top N coins by 24h volume</label><input type="number" name="n" value="20" min="2" max="60"></div>'
            f'<div><label>Timeframe</label>{tf_select("tf", False)}</div><div><label>Processes</label><input type="number" name="jobs" value="1" min="1" max="4"></div></div>'
            f'<div class="checks">{checkbox("ml", "ML meta-labeler", False)}{checkbox("market", "Crypto-wide + macro layer", True)}'
            f'{checkbox("futures", "Futures data", True)}{checkbox("notify", "Send alert", False)}</div><button>Run scan</button></form>')


def home_page(cfg: WebConfig, manager: jobs.JobManager, scheduler, warnings: list[str], error: str = "", status: int = 200):
    rows = manager.list(8)
    reports = report_rows(cfg)
    latest = latest_per_coin(reports)
    live = manager.live_counts()
    free_gb = shutil.disk_usage(cfg.data_dir).free / 1e9 if cfg.data_dir.exists() else 0
    notify_on = jobs.notify_configured()
    n_audits = sum(1 for r in reports if r["kind"] == "audit")
    tiles = "".join(f'<div class="tile"><div class="label">{E(a)}</div><div class="value">{E(str(b))}</div><div class="delta">{E(c)}</div></div>'
                    for a, b, c in [("Jobs", f"{live['running']} running", f"{live['queued']} waiting"),
                                    ("Audits stored", n_audits, f"kept {cfg.keep_days:g} days (newest per coin always)"),
                                    ("Disk free", f"{free_gb:.1f} GB", str(cfg.data_dir)),
                                    ("Alerts", "on" if notify_on else "off", "Discord / Telegram" if notify_on else "set DISCORD_WEBHOOK_URL to enable")])
    banners = "".join(f'<div class="banner">{E(w)}</div>' for w in warnings)
    if error:
        banners += f'<div class="banner err">{E(error)}</div>'
    if free_gb < 0.2 and cfg.data_dir.exists():
        banners += '<div class="banner err">Less than 200 MB of disk left — scheduled runs are paused. Increase the disk size or lower COIN_AUDIT_KEEP_DAYS.</div>'
    if scheduler and scheduler.schedules:
        sched = ('<div class="wrap tight"><table><thead><tr><th>Name</th><th>Runs</th><th>Last run (UTC)</th><th>Settings</th></tr></thead><tbody>'
                 + "".join(f'<tr><td>{E(r["name"])}</td><td>{E(r["when"])}</td><td>{E(r["last_run"])}</td><td class="muted">{E(json.dumps(r["params"]))}</td></tr>'
                           for r in scheduler.status()) + "</tbody></table></div>")
    else:
        sched = ('<p class="muted">Nothing scheduled. Set <code>COIN_AUDIT_WATCHLIST=SOL,ARB</code> (and optionally '
                 '<code>COIN_AUDIT_WATCH_TF=4h</code>) in the Environment tab to audit those coins after every candle close.</p>')
    cards = f'<div class="cards">{"".join(audit_card(r) for r in latest)}</div>' if latest else \
        '<p class="muted">No audits yet — run the demo check or audit a coin above.</p>'
    body = f"""<h1>Coin Audit</h1><p class="sub">Read-only research on Binance Spot coins: a score, a verdict, a trade plan drawn on the chart — validated, not promised.</p>
{banners}<div class="grid2">{audit_form()}{scan_form()}</div>
<h2>Latest audits</h2>{cards}
<h2>Status</h2><div class="tiles">{tiles}</div>
<h2>Maintenance</h2><div class="actions">
<form method="post" action="/jobs"><input type="hidden" name="kind" value="demo"><button class="secondary">Demo check (synthetic data)</button></form>
<form method="post" action="/jobs"><input type="hidden" name="kind" value="review"><button class="secondary">Review past audits</button></form>
<form method="post" action="/dashboard/rebuild"><button class="secondary">Rebuild dashboard file</button></form></div>
<h2>Schedule</h2>{sched}<h2>Recent jobs</h2>{jobs_table(rows)}<p><a href="/jobs">all jobs →</a></p>"""
    resp = page("Home", body, active="home")
    resp.status_code = status
    return resp


def render_markdown(text: str) -> str:
    """Markdown -> HTML with raw HTML neutralised: python-markdown passes tags through, so every '<' in the source is
    escaped first (our reports never contain HTML; the CSP would block a script anyway, but it must not even render)."""
    out = md_lib.markdown(text.replace("<", "&lt;"), extensions=["tables", "fenced_code", "sane_lists"])
    return out.replace("<table>", '<div class="wrap tight"><table>').replace("</table>", "</table></div>")


def inline_script_csp(doc: str) -> str:
    """A CSP that allows exactly the inline scripts a stored chart page carries (its renderer of the day)."""
    hashes = []
    for m in re.finditer(r"<script>(.*?)</script>", doc, re.S):
        hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode("ascii") + "'")
    allowed = " ".join(hashes) or "'none'"
    return (f"default-src 'none'; style-src 'unsafe-inline'; script-src {allowed}; img-src data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def load_json(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def report_header(name: str, a: dict | None, files: set) -> str:
    base = name[:-3]
    links = [f'<a class="btn secondary small" href="/reports/{E(name)}?raw=1">raw markdown</a>']
    if f"{base}.json" in files:
        links.insert(0, f'<a class="btn secondary small" href="/reports/{E(base)}.json">JSON</a>')
    if f"{base}_chart.html" in files:
        links.insert(0, f'<a class="btn secondary small" href="/reports/{E(base)}_chart.html">chart page</a>')
    if f"{base}_tearsheet.html" in files:
        links.append(f'<a class="btn secondary small" href="/reports/{E(base)}_tearsheet.html">tearsheet</a>')
    m = REPORT_NAME_RE.match(base)
    if not a or not m:
        title = "Market scan" if base.startswith("scan_") else base
        return f'<h1>{E(title)}</h1><div class="actions">{"".join(links)}</div>'
    p, sig = a.get("plan") or {}, a.get("signals") or []
    approved = [s["strategy_id"] for s in sig if s.get("decision") == "APPROVED"]
    rerun = (f'<form method="post" action="/jobs"><input type="hidden" name="kind" value="audit"><input type="hidden" name="coins" value="{E(a.get("base") or m.group(1))}">'
             f'<input type="hidden" name="tf" value="{E(a.get("timeframe") or m.group(2))}"><input type="hidden" name="ml" value="on">'
             f'<input type="hidden" name="market" value="on"><input type="hidden" name="futures" value="on"><button class="small">Re-run this audit</button></form>')
    items = [("Verdict", verdict_badge(a.get("verdict", ""), a.get("score")), ""),
             ("Last close", _px(p.get("price")), ""), ("Entry zone", f'{_px(p.get("entry_low"))} – {_px(p.get("entry_high"))}', ""),
             ("Stop", f'{_px(p.get("stop"))} ({E(str(p.get("stop_distance_pct", "")))}%)', "down"),
             ("Target 1 / 2", f'{_px(p.get("target1"))} / {_px(p.get("target2"))}', "up"),
             ("Approved signals", E(", ".join(approved)) if approved else '<span class="muted">none</span>', ""),
             ("Audited", f'{E(a.get("audit_time_utc", ""))} UTC', "")]
    summary = "".join(f'<div><div class="k">{E(k)}</div><div class="v {c}">{v}</div></div>' for k, v, c in items)
    flags = "".join(f'<div class="banner">{E(f)}</div>' for f in a.get("flags", []))
    return (f'<h1>{E(a.get("symbol", ""))} · {E(a.get("timeframe", ""))}</h1><p class="sub">{E(a.get("verdict_text", ""))}</p>'
            f'<div class="card"><div class="summary">{summary}</div><div class="actions">{"".join(links)}{rerun}</div></div>{flags}')


def reports_page(cfg: WebConfig) -> HTMLResponse:
    rows = report_rows(cfg)
    audits = [r for r in rows if r["kind"] == "audit"]
    others = [r for r in rows if r["kind"] != "audit"]

    def links(r):
        out = [f'<a href="/reports/{E(r["md"])}">report</a>']
        if r["chart"]:
            out.append(f'<a href="/reports/{E(r["base"])}_chart.html">chart</a>')
        if r["json"]:
            out.append(f'<a href="/reports/{E(r["base"])}.json">json</a>')
        if r["tearsheet"]:
            out.append(f'<a href="/reports/{E(r["base"])}_tearsheet.html">tearsheet</a>')
        return " · ".join(out)
    body = "".join(f'<tr data-key="{E(r["symbol"].lower())} {E(r["tf"])} {E(r["verdict"].lower())}"><td><a href="/reports/{E(r["md"])}"><b>{E(r["symbol"])}</b></a> <span class="rel">{E(r["tf"])}</span></td>'
                   f'<td>{E(r["when"])}</td><td>{verdict_badge(r["verdict"], r["score"]) if r["verdict"] else "<span class=muted>—</span>"}</td>'
                   f'<td class="num">{E(str(r["approved"]))}</td><td>{links(r)}</td></tr>' for r in audits[:400])
    table = (f'<input class="filter" id="filter" type="text" placeholder="filter: coin, timeframe or verdict" autocomplete="off">'
             f'<div class="wrap"><table id="reports"><thead><tr><th>Coin</th><th>Audited (UTC)</th><th>Verdict · score</th><th>Approved</th><th>Files</th></tr></thead>'
             f'<tbody>{body or "<tr><td colspan=5>No audits yet.</td></tr>"}</tbody></table></div>')
    other = ""
    if others:
        other = ('<h2>Market scans and other files</h2><div class="wrap tight"><table><thead><tr><th>File</th><th>Modified (UTC)</th><th>Files</th></tr></thead><tbody>'
                 + "".join(f'<tr><td><a href="/reports/{E(r["md"])}">{E(r["base"])}</a></td><td>{E(r["when"] or r["modified"])}</td><td>{links(r)}</td></tr>' for r in others[:100])
                 + "</tbody></table></div>")
    return page("Reports", f'<h1>Reports</h1><p class="sub">{len(audits)} audits on disk · the newest report per coin is never pruned.</p>{table}{other}', active="reports")


# --------------------------------------------------------------------- app
def create_app(cfg: WebConfig | None = None, manager: jobs.JobManager | None = None, scheduler: jobs.Scheduler | None = None,
               start_background: bool = True, http_get=requests.get) -> FastAPI:
    cfg = cfg or WebConfig.from_env()
    if manager is None:
        manager = jobs.JobManager(cfg.data_dir, AUDIT_PY, max_concurrent=cfg.max_jobs, max_queue=cfg.max_queue)
    warnings: list[str] = []
    if scheduler is None:
        schedules, warnings = jobs.schedules_from_env()
        scheduler = jobs.Scheduler(manager, schedules, cfg.data_dir / "scheduler.json", reports_dir=cfg.reports, keep_days=cfg.keep_days)

    @asynccontextmanager
    async def lifespan(app):
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if start_background:
            manager.start()
            scheduler.start()
        yield
        if start_background:
            scheduler.stop()
            manager.stop()

    app = FastAPI(title="Coin Audit", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    throttle = Throttle()
    app.state.cfg, app.state.manager, app.state.scheduler, app.state.throttle = cfg, manager, scheduler, throttle

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return secure(await call_next(request))
        if not cfg.password and not cfg.allow_anonymous:
            return secure(PlainTextResponse(NO_PASSWORD, status_code=503))
        if cfg.password:
            ip = client_ip(request, cfg.trust_proxy, cfg.client_ip_header)
            if throttle.blocked(ip):
                return secure(PlainTextResponse("Too many wrong passwords from this address. Try again in ten minutes.",
                                                status_code=429, headers={"Retry-After": "600"}))
            cred = basic_credentials(request)
            if not credentials_ok(cred, cfg):
                if cred is not None:
                    throttle.fail(ip)
                return secure(PlainTextResponse("Login required", status_code=401,
                                                headers={"WWW-Authenticate": 'Basic realm="Coin Audit", charset="UTF-8"'}))
            throttle.ok(ip)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request, cfg):
            return secure(PlainTextResponse("Cross-site request refused.", status_code=403))
        return secure(await call_next(request))

    async def form(request: Request) -> dict:
        if int(request.headers.get("content-length") or 0) > 20_000:
            raise ValueError("form too large")
        body = (await request.body()).decode("utf-8", "replace")
        return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}

    def report_files() -> set:
        return {p.name for p in cfg.reports.iterdir() if p.is_file()} if cfg.reports.exists() else set()

    # ---- public
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__, "jobs": manager.live_counts()}

    @app.get("/static/app.js")
    def app_js():
        return Response(APP_JS, media_type="application/javascript")

    @app.get("/static/chart.js")
    def chart_js():
        return Response(chart.CHART_JS, media_type="application/javascript")

    # ---- pages
    @app.get("/", response_class=HTMLResponse)
    def home():
        return home_page(cfg, manager, scheduler, warnings)

    @app.post("/jobs")
    async def create_job(request: Request):
        try:
            f = await form(request)
            kind = f.get("kind", "")
            params = {k: f.get(k, "") for k in ("coins", "tf", "n", "jobs", "strategies")}
            for k in ("ml", "market", "futures", "tearsheet", "notify"):                # unchecked box = field absent
                params[k] = k in f
            if params["notify"] and not jobs.notify_configured():
                raise jobs.JobError("Alerts are not configured: set DISCORD_WEBHOOK_URL (or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).")
            job = manager.submit(kind, params, source="web")
        except (jobs.JobError, jobs.QueueFull, ValueError) as e:
            return home_page(cfg, manager, scheduler, warnings, error=str(e), status=400)
        return RedirectResponse(f"/jobs/{job['id']}", status_code=303)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page():
        rows = manager.list(100)
        return page("Jobs", f'<h1>Jobs</h1><p class="sub">Every audit runs as its own process; the log below each job is what it printed.</p>{jobs_table(rows)}', active="jobs")

    def job_view(job: dict) -> dict:
        files = report_files()
        charts = [f"{n[:-3]}_chart.html" for n in job.get("reports", []) if n.endswith(".md") and f"{n[:-3]}_chart.html" in files]
        return {**job, "charts": charts, "log": manager.log_tail(job["id"])}

    @app.get("/jobs/{jid}", response_class=HTMLResponse)
    def job_page(jid: str):
        job = manager.get(jid)
        if not job:
            return secure(PlainTextResponse("No such job", status_code=404))
        running = job["status"] not in jobs.TERMINAL
        v = job_view(job)
        links = "".join(f'<a class="btn small" href="/reports/{E(n)}">📄 {E(n[:-3])}</a> ' for n in v["reports"])
        links += "".join(f'<a class="btn secondary small" href="/reports/{E(n)}">📈 chart</a> ' for n in v["charts"])
        body = (f'<div id="job" data-id="{E(jid)}" data-status="{E(job["status"])}"><h1>{E(job["label"])}</h1>'
                f'<p><span id="status">{badge(job["status"])}</span> <span class="muted">created {E(job["created"])} UTC · started by {E(job["source"])}'
                f' · <span id="elapsed"></span></span></p><p class="stage" id="stage"></p>'
                f'<p id="err" class="bad">{E(job["error"])}</p><div class="actions" id="links">{links or "<span class=muted>No report yet.</span>"}</div>'
                f'<h2>Log</h2><pre class="log" id="log">{E(manager.log_tail(jid))}</pre>'
                + (f'<form method="post" action="/jobs/{E(jid)}/cancel"><button class="secondary" id="cancel">Cancel this job</button></form>' if running else "")
                + '</div>')
        return page(job["label"], body, active="jobs")

    @app.post("/jobs/{jid}/cancel")
    def cancel_job(jid: str):
        manager.cancel(jid)
        return RedirectResponse(f"/jobs/{jid}" if manager.get(jid) else "/jobs", status_code=303)

    @app.get("/api/jobs")
    def api_jobs():
        return JSONResponse(manager.list(100))

    @app.get("/api/jobs/{jid}")
    def api_job(jid: str):
        job = manager.get(jid)
        if not job:
            return JSONResponse({"error": "no such job"}, status_code=404)
        return JSONResponse(job_view(job))

    @app.get("/connectivity", response_class=HTMLResponse)
    def connectivity_page():
        rows = check_connectivity(http_get)
        required = rows[0]
        head = ('<div class="banner ok">Binance market data is reachable from this server. You are good to go.</div>' if required["ok"] else
                '<div class="banner err"><b>This server cannot reach Binance market data.</b> If it says HTTP 451 the provider blocks this '
                'server\'s country. A Render service cannot change region after it is created: create a new one in Frankfurt or Singapore.</div>')
        body = "".join(f'<tr><td>{E(r["name"])}</td><td class="{"ok" if r["ok"] else "bad"}">{"✔" if r["ok"] else "✖"} {E(r["verdict"])}</td>'
                       f'<td class="num">{E("" if r["ms"] is None else str(r["ms"]) + " ms")}</td><td class="muted">{E(r["purpose"])}</td></tr>' for r in rows)
        return page("Connectivity", f'<h1>Can this server reach the data sources?</h1>{head}<div class="wrap"><table><thead><tr><th>Source</th><th>Result</th><th>Time</th><th>Used for</th></tr></thead><tbody>{body}</tbody></table></div>'
                    '<p class="muted">Optional sources can fail: the audit then drops that layer and says so in the report.</p>', active="connectivity")

    @app.get("/api/connectivity")
    def api_connectivity():
        return JSONResponse(check_connectivity(http_get))

    # ---- reports
    @app.get("/reports", response_class=HTMLResponse)
    def reports_index():
        return reports_page(cfg)

    @app.get("/reports/{name}")
    def report(name: str, raw: int = 0):
        p = cfg.reports / name
        if not NAME_RE.match(name) or not p.is_file():
            return secure(PlainTextResponse("Not found", status_code=404))
        if name.endswith(".md") and not raw:
            files = report_files()
            a = load_json(p.with_suffix(".json")) if f"{name[:-3]}.json" in files else None
            chart_html = chart.embed_html(a["chart"]) if a and isinstance(a.get("chart"), dict) else ""
            md = render_markdown(p.read_text(encoding="utf-8", errors="replace"))
            return page(name[:-3], f'{report_header(name, a, files)}{chart_html}<div class="md">{md}</div>', active="reports")
        if name.endswith(".json"):
            return secure(FileResponse(p, media_type="application/json"))
        if name.endswith("_chart.html"):
            # Prefer re-rendering from the report's JSON with today's renderer (one script, served from /static);
            # a chart page without its JSON is served as stored, allowing exactly the script it carries.
            a = load_json(cfg.reports / (name[:-11] + ".json"))
            if a and isinstance(a.get("chart"), dict):
                ch = a["chart"]
                back = f'<p class="sub"><a href="/reports/{E(name[:-11])}.md">← full report</a></p>'
                return page(f'{ch.get("symbol", "")} {ch.get("timeframe", "")} chart',
                            f'<h1>{E(ch.get("symbol", ""))} · {E(ch.get("timeframe", ""))} — {E(str(ch.get("verdict", "")))} {E(str(ch.get("score", "")))}/100</h1>'
                            f'{back}{chart.embed_html(ch)}', active="reports")
            doc = p.read_text(encoding="utf-8", errors="replace")
            return secure(HTMLResponse(doc), inline_script_csp(doc))
        if name.endswith(".html"):
            return secure(FileResponse(p, media_type="text/html"), SANDBOX_CSP if name.endswith("_tearsheet.html") else STRICT_CSP)
        return secure(FileResponse(p, media_type="text/plain; charset=utf-8"))

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page():
        body = dashboard.body(cfg.reports, cfg.audits_csv, cfg.signals_csv, link_prefix="/reports/")
        return page("Dashboard", body + '<form method="post" action="/dashboard/rebuild"><button class="secondary small">Rebuild the static dashboard.html</button></form>',
                    active="dashboard")

    @app.post("/dashboard/rebuild")
    def dashboard_rebuild():
        dashboard.build(cfg.reports, cfg.audits_csv, cfg.signals_csv)
        return RedirectResponse("/dashboard", status_code=303)

    return app


app = create_app()
