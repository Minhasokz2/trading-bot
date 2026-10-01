"""Hosted web app (v7): a small SaaS-style console — overview, audits, coin pages with live charts, an editable
watchlist that drives the scheduler, settings, reports, jobs. Password protected.

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

import binance_client as bc
import chart
import dashboard
import jobs
import settings as cfgmod
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
LIVE_STREAM_HOST = "wss://data-stream.binance.vision"      # Binance's public market-data stream (charts stay live)
STRICT_CSP = (f"default-src 'none'; style-src 'unsafe-inline'; script-src 'self' '{THEME_JS_HASH}'; "
              f"connect-src 'self' {LIVE_STREAM_HOST}; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
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

    @property
    def watchlist_path(self) -> Path:
        return self.data_dir / "watchlist.json"

    @property
    def user_settings_path(self) -> Path:
        return self.data_dir / "user_settings.json"

    @property
    def settings_toml(self) -> Path:
        return self.data_dir / "settings.toml"


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
.app { display:flex; min-height:100vh; }
.side { width:224px; flex:0 0 224px; position:sticky; top:0; height:100vh; display:flex; flex-direction:column; background:var(--surface);
  border-right:1px solid var(--border); padding:14px 12px; z-index:6; }
.brand { font-weight:700; font-size:16px; color:var(--ink); display:inline-flex; align-items:center; gap:9px; padding:4px 8px 14px; }
.brand:hover { text-decoration:none; }
.brand .logo { width:24px; height:24px; border-radius:7px; background:linear-gradient(135deg, var(--accent), var(--good)); display:inline-block; }
.side nav { display:flex; flex-direction:column; gap:2px; }
.side nav a { padding:8px 10px; border-radius:7px; color:var(--ink2); font-weight:600; font-size:13px; display:flex; align-items:center; gap:10px; }
.side nav a .ic { width:18px; text-align:center; color:var(--muted); }
.side nav a:hover { color:var(--ink); background:var(--plane); text-decoration:none; }
.side nav a[aria-current=page] { color:var(--ink); background:var(--plane); box-shadow:inset 3px 0 0 var(--accent); }
.side nav a[aria-current=page] .ic { color:var(--accent); }
.side-foot { margin-top:auto; padding:10px 8px 0; color:var(--muted); font-size:12px; display:flex; align-items:center; gap:8px; }
.theme { background:transparent; border:1px solid var(--border); color:var(--ink2); border-radius:6px; padding:4px 9px; font:inherit; font-size:12px; cursor:pointer; margin:0; }
.main { flex:1; min-width:0; display:flex; flex-direction:column; }
.topbar { position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:12px; padding:10px 20px; background:var(--surface); border-bottom:1px solid var(--border); }
.burger { display:none; background:transparent; border:1px solid var(--border); color:var(--ink); border-radius:6px; padding:5px 9px; font:inherit; margin:0; cursor:pointer; }
form.quick { display:flex; gap:6px; flex:1; max-width:560px; margin:0; }
form.quick input[type=text] { flex:1; min-width:0; font-weight:600; text-transform:uppercase; }
form.quick select { width:auto; } form.quick button { margin:0; padding:8px 14px; }
.topbar .counts { color:var(--ink2); font-size:12px; margin-left:auto; white-space:nowrap; }
.page { max-width:1240px; width:100%; margin:0 auto; padding:22px 20px 40px; }
.page > h1 { font-size:22px; margin:2px 0 4px; } .page > .sub { margin-bottom:18px; }
h2 { font-size:15px; margin:24px 0 8px; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:16px; align-items:start; }
.grid3 { display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:16px; align-items:start; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px 16px 16px; margin:0; }
.card h2 { margin:0 0 6px; font-size:15px; } .card .hint { color:var(--ink2); font-size:12px; margin:0 0 6px; }
.card h4 { margin:12px 0 4px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink2); }
label { display:block; font-size:12px; color:var(--ink2); margin:10px 0 3px; }
input[type=text], input[type=number], select { width:100%; padding:8px 10px; border:1px solid var(--border); border-radius:6px;
  background:var(--plane); color:var(--ink); font:inherit; }
input.big { font-size:17px; font-weight:600; text-transform:uppercase; letter-spacing:.03em; }
.row { display:grid; grid-template-columns:minmax(0, 2fr) minmax(0, 1fr); gap:12px; align-items:end; }
.row3 { grid-template-columns:repeat(3, minmax(0, 1fr)); } .row2 { grid-template-columns:repeat(2, minmax(0, 1fr)); }
details.adv { margin-top:10px; } details.adv summary { cursor:pointer; color:var(--ink2); font-size:13px; }
.checks { display:flex; flex-wrap:wrap; gap:4px 16px; margin-top:4px; }
.checks label { display:inline-flex; align-items:center; gap:6px; margin:6px 0 0; color:var(--ink); font-size:13px; }
button, .btn { background:var(--accent); color:#fff; border:0; border-radius:6px; padding:9px 16px; font:inherit; font-weight:600;
  cursor:pointer; margin-top:12px; display:inline-block; text-decoration:none; line-height:1.2; }
button:hover, .btn:hover { filter:brightness(1.08); text-decoration:none; }
button.secondary, .btn.secondary { background:transparent; color:var(--ink); border:1px solid var(--border); margin-top:0; }
button.danger { background:transparent; color:var(--critical); border:1px solid var(--critical); margin-top:0; }
button.small, .btn.small { padding:5px 10px; font-size:12px; margin-top:0; }
.inline { display:inline; margin:0; } .inline button { margin:0; }
.badge { display:inline-flex; align-items:center; gap:6px; padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600;
  border:1px solid var(--border); white-space:nowrap; color:var(--ink2); }
.badge.done { color:var(--good); border-color:var(--good); } .badge.failed, .badge.timeout { color:var(--critical); border-color:var(--critical); }
.badge.running { color:var(--accent); border-color:var(--accent); } .badge.queued { color:var(--warning); }
.spin { width:10px; height:10px; border:2px solid var(--accent); border-right-color:transparent; border-radius:50%; display:inline-block; animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.chip { display:inline-flex; align-items:center; padding:3px 11px; border-radius:12px; font-size:12px; font-weight:700; letter-spacing:.02em; white-space:nowrap; }
.chip.good { background:rgba(12,163,12,.14); color:var(--good); } .chip.warning { background:rgba(250,178,25,.16); color:#b7810a; }
.chip.critical { background:rgba(208,59,59,.14); color:var(--critical); } .chip.muted { background:var(--plane); color:var(--ink2); }
:root[data-theme="dark"] .chip.warning { color:var(--warning); } @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .chip.warning { color:var(--warning); } }
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
.brief .action { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:flex-start; padding:12px 14px; border-radius:8px; background:var(--plane); }
.brief .action p { flex:1 1 260px; }
.brief .action p { margin:0; font-size:15px; } .brief .setup { color:var(--ink); margin:12px 0 4px; line-height:1.55; }
.brief ul { margin:4px 0 0 18px; padding:0; } .brief li { margin:3px 0; }
.brief .cols { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:4px 24px; }
.brief .conf { margin:12px 0 0; color:var(--ink2); font-size:13px; } .brief details { margin-top:8px; } .brief summary { cursor:pointer; color:var(--ink2); font-size:13px; }
.score { display:inline-flex; align-items:center; gap:8px; font-variant-numeric:tabular-nums; font-weight:600; }
.summary { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px 16px; margin:12px 0 4px; }
.summary .k { color:var(--ink2); font-size:12px; } .summary .v { font-size:15px; font-weight:600; font-variant-numeric:tabular-nums; }
.summary .v.up { color:var(--good); } .summary .v.down { color:var(--critical); } .summary .meter { width:64px; }
.stage { color:var(--ink2); font-size:13px; margin:6px 0; } .filter { max-width:320px; margin:0 0 10px; }
.wrap table { min-width:640px; } .wrap.tight table { min-width:0; }
.muted { color:var(--ink2); } .ok { color:var(--good); } .bad { color:var(--critical); } .rel { color:var(--muted); font-size:12px; }
.tile .delta { overflow-wrap:anywhere; } .tile .value.sm { font-size:19px; margin-top:6px; } .jobs td:first-child { max-width:380px; }
.inline select { width:auto; display:inline-block; padding:5px 8px; }
td.sym a { font-weight:700; } td.sym .tf { color:var(--ink2); font-size:12px; margin-left:4px; }
.price { font-variant-numeric:tabular-nums; white-space:nowrap; }
.empty { padding:26px; text-align:center; color:var(--ink2); background:var(--surface); border:1px dashed var(--border); border-radius:10px; }
.toggle { display:inline-flex; gap:4px; }
@media (max-width:900px) {
  .side { position:fixed; left:0; top:0; transform:translateX(-100%); transition:transform .18s ease; box-shadow:4px 0 24px rgba(0,0,0,.3); }
  .side.open { transform:none; } .burger { display:inline-block; } .row, .row3, .row2 { grid-template-columns:1fr; }
  .page { padding:14px 12px 32px; } .topbar { padding:8px 12px; } form.quick { max-width:none; } form.quick input[type=text] { min-width:110px; } }
"""
STATUS_LABEL = {"queued": "⏳ queued", "running": "▶ running", "done": "✔ done", "failed": "✖ failed",
                "cancelled": "⊘ cancelled", "timeout": "⏱ timed out"}
NAV_ITEMS = [("home", "/", "Overview", "⌂"), ("audit", "/audit", "Audit a coin", "⚡"), ("watchlist", "/watchlist", "Watchlist", "☆"),
             ("reports", "/reports", "Reports", "▤"), ("dashboard", "/dashboard", "Dashboard", "▦"), ("jobs", "/jobs", "Jobs", "⏱"),
             ("settings", "/settings", "Settings", "⚙"), ("connectivity", "/connectivity", "Connectivity", "⇅")]
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
CURRENT = ' aria-current="page"'
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
COIN_PATH_RE = re.compile(r"^[A-Z0-9]{2,20}$")
ACTION_TONE = {code: tone for code, (label, tone) in __import__("brief").ACTIONS.items()}


def page(title: str, body: str, csp: str = STRICT_CSP, active: str = "", counts: dict | None = None) -> HTMLResponse:
    nav = "".join(f'<a href="{href}"{CURRENT if key == active else ""}><span class="ic">{ic}</span>{label}</a>'
                  for key, href, label, ic in NAV_ITEMS)
    live = f"{counts['running']} running · {counts['queued']} waiting" if counts else ""
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<title>{E(title)} · Coin Audit</title><script>{THEME_JS}</script><style>{dashboard.CSS}{chart.CSS}{APP_CSS}</style></head><body>'
           f'<div class="app"><aside class="side" id="side"><a class="brand" href="/"><span class="logo"></span>Coin Audit</a><nav>{nav}</nav>'
           f'<div class="side-foot">v{E(__version__)}<button class="theme" id="theme" type="button" title="switch light / dark">◐ theme</button></div></aside>'
           f'<div class="main"><header class="topbar"><button class="burger" id="burger" type="button" aria-label="menu">☰</button>'
           f'{quick_form()}<span class="counts" id="counts">{E(live)}</span></header>'
           f'<main class="page">{body}</main>'
           f'<p class="foot page">Coin Audit {E(__version__)} · read-only Binance Spot research — no orders, no API keys · not financial advice</p></div></div>'
           f'<script src="/static/app.js"></script></body></html>')
    return secure(HTMLResponse(doc), csp)


def quick_form() -> str:
    return (f'<form class="quick" method="post" action="/jobs"><input type="hidden" name="kind" value="audit"><input type="hidden" name="ml" value="on">'
            f'<input type="hidden" name="market" value="on"><input type="hidden" name="futures" value="on">'
            f'<input type="text" name="coins" placeholder="Audit any coin… e.g. SOL" maxlength="120" autocomplete="off" autocapitalize="characters" required>'
            f'{tf_select("tf", False)}<button type="submit">Audit</button></form>')


def badge(status: str) -> str:
    spin = '<span class="spin"></span>' if status == "running" else ""
    return f'<span class="badge {E(status)}">{spin}{E(STATUS_LABEL.get(status, status))}</span>'


def verdict_badge(verdict: str, score=None) -> str:
    cls, icon = dashboard.VERDICT.get(verdict or "", ("muted", "○"))
    s = "" if score in (None, "") else f' <span class="score"><span class="meter" title="score {E(str(score))}/100"><i style="width:{_pct(score)}%"></i></span>{_num(score)}</span>'
    return f'<span class="v {cls}"><span class="dot"></span>{icon} {E(verdict or "—")}</span>{s}'


def action_chip(action: dict | None) -> str:
    if not action:
        return '<span class="chip muted">—</span>'
    return f'<span class="chip {E(action.get("tone", "muted"))}" title="{E(action.get("text", ""))}">{E(action.get("label", ""))}</span>'


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
    """'3 min ago' for lists; the exact UTC time stays in the title."""
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


# ------------------------------------------------------------ audit catalogue
class AuditIndex:
    """Small summaries of every audit JSON on disk, cached by file mtime so pages never re-parse unchanged reports."""

    KEYS = ("symbol", "base", "quote", "timeframe", "audit_time_utc", "verdict", "score", "flags", "chart_file", "report_path")

    def __init__(self, reports_dir: Path):
        self.dir = Path(reports_dir)
        self._cache: dict = {}

    def _summary(self, p: Path) -> dict | None:
        a = load_json(p)
        if not a or "symbol" not in a or "timeframe" not in a:
            return None
        plan, b = a.get("plan") or {}, a.get("brief") or {}
        approved = [s["strategy_id"] for s in a.get("signals") or [] if s.get("decision") == "APPROVED"]
        return {**{k: a.get(k) for k in self.KEYS}, "base": p.stem, "md": p.with_suffix(".md").name,
                "when": a.get("audit_time_utc", ""), "tf": a.get("timeframe", ""), "action": b.get("action"),
                "headline": b.get("headline", ""), "price": plan.get("price"), "entry_low": plan.get("entry_low"),
                "entry_high": plan.get("entry_high"), "stop": plan.get("stop"), "target1": plan.get("target1"),
                "approved": approved, "has_chart": isinstance(a.get("chart"), dict), "offline": bool(a.get("offline"))}

    def all(self) -> list[dict]:
        if not self.dir.exists():
            return []
        out, seen = [], set()
        for p in self.dir.glob("*.json"):
            if not REPORT_NAME_RE.match(p.stem):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            seen.add(p.name)
            hit = self._cache.get(p.name)
            if not hit or hit[0] != mtime:
                hit = (mtime, self._summary(p))
                self._cache[p.name] = hit
            if hit[1]:
                out.append(hit[1])
        for k in list(self._cache):
            if k not in seen:
                del self._cache[k]
        out.sort(key=lambda s: s["when"], reverse=True)
        return out

    def latest(self) -> list[dict]:
        """The newest audit per (symbol, timeframe), best score first."""
        seen, out = set(), []
        for s in self.all():
            key = (s["symbol"], s["tf"])
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        out.sort(key=lambda s: (-(s.get("score") or 0), s["symbol"]))
        return out

    def for_symbol(self, symbol: str) -> list[dict]:
        return [s for s in self.all() if s["symbol"] == symbol]


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


def score_history(path: Path, symbol: str, tf: str | None = None) -> list[tuple[str, float, str]]:
    out = []
    if not path.exists():
        return out
    try:
        for r in csv.DictReader(path.open(encoding="utf-8")):
            if r.get("symbol") == symbol and (tf is None or r.get("timeframe") == tf):
                out.append((r.get("audit_time_utc", ""), float(r.get("score") or 0), r.get("verdict", "")))
    except (OSError, csv.Error, ValueError):
        pass
    return sorted(out)


def report_rows(cfg: WebConfig, index: AuditIndex | None = None) -> list[dict]:
    """One row per audit or scan on disk (newest first), joined with audits.csv and the cached summaries."""
    if not cfg.reports.exists():
        return []
    files = {p.name: p for p in cfg.reports.iterdir() if p.is_file() and NAME_RE.match(p.name)}
    log = audit_log(cfg.audits_csv)
    summaries = {s["base"]: s for s in index.all()} if index else {}
    rows = []
    for name, p in files.items():
        if not name.endswith(".md") or name == "dashboard.md":
            continue
        base = name[:-3]
        row = {"base": base, "md": name, "json": f"{base}.json" in files, "chart": f"{base}_chart.html" in files,
               "tearsheet": f"{base}_tearsheet.html" in files, "mtime": p.stat().st_mtime,
               "modified": time.strftime("%Y-%m-%d %H:%M", time.gmtime(p.stat().st_mtime)), "kind": "file",
               "symbol": "", "tf": "", "when": "", "verdict": "", "score": "", "approved": "", "log": None, "action": None}
        m = REPORT_NAME_RE.match(base)
        s = SCAN_NAME_RE.match(base)
        if m:
            row.update(kind="audit", symbol=m.group(1), tf=m.group(2), when=f"{m.group(3)} {m.group(4)[:2]}:{m.group(4)[2:]}")
            lr = log.get(base) or log.get(re.sub(r"_r\d+$", "", base))
            if lr:
                row.update(verdict=lr.get("verdict", ""), score=lr.get("score", ""), approved=lr.get("approved_signals", ""), log=lr)
            sm = summaries.get(base)
            if sm:
                row.update(verdict=sm.get("verdict") or row["verdict"], score=sm.get("score", row["score"]), action=sm.get("action"),
                           approved=len(sm.get("approved") or []))
        elif s:
            row.update(kind="scan", symbol="Market scan", tf=s.group(1), when=f"{s.group(2)} {s.group(3)[:2]}:{s.group(3)[2:]}")
        rows.append(row)
    rows.sort(key=lambda r: (r["when"] or "", r["mtime"]), reverse=True)
    return rows


# ----------------------------------------------------------------- components
def brief_card(b: dict, compact: bool = False) -> str:
    a = b.get("action") or {}
    li = lambda xs: "".join(f"<li>{E(x)}</li>" for x in xs)  # noqa: E731
    head = (f'<div class="action"><span class="chip {E(a.get("tone", "muted"))}">{E(a.get("label", ""))}</span><p>{E(a.get("text", ""))}</p></div>'
            f'<p class="setup">{E(b.get("summary", ""))}</p>')
    cols = (f'<div class="cols"><div><h4>The plan</h4><ul>{li(b.get("plan", []))}</ul></div><div>'
            + (f'<h4>What would make this a buy</h4><ul>{li(b["would_buy"])}</ul>' if b.get("would_buy") else "")
            + f'<h4>What would kill it</h4><ul>{li(b.get("would_kill", []))}</ul></div></div>')
    more = ""
    if not compact:
        more = (f'<details><summary>For / against · strategies with an edge · risk notes</summary>'
                f'<h4>For</h4><ul>{li(b.get("plus") or ["nothing stands out"])}</ul><h4>Against</h4><ul>{li(b.get("minus") or ["nothing stands out"])}</ul>'
                f'<h4>Strategies with an edge on this coin</h4><ul>{li(b.get("edge", []))}</ul>'
                + (f'<h4>Risk notes</h4><ul>{li(b["risk"])}</ul>' if b.get("risk") else "") + "</details>")
    c = b.get("confidence") or {}
    return f'<div class="card brief">{head}{cols}{more}<p class="conf">Confidence <b>{E(c.get("level", ""))}</b> — {E(c.get("text", ""))}</p></div>'


def audit_form(coins: str = "", tf: str = "4h") -> str:
    adv = (f'<details class="adv"><summary>Options</summary><div class="checks">{checkbox("ml", "ML meta-labeler", True)}'
           f'{checkbox("market", "Crypto-wide + macro layer", True)}{checkbox("futures", "Futures data (funding, OI)", True)}'
           f'{checkbox("tearsheet", "HTML tearsheet (slow)", False)}{checkbox("notify", "Send alert", False)}</div>'
           f'<label>Only these strategies (optional: ids, families or name fragments, comma separated)</label>'
           f'<input type="text" name="strategies" maxlength="200" placeholder="all 27"></details>')
    return (f'<form class="card" method="post" action="/jobs"><input type="hidden" name="kind" value="audit">'
            f'<h2>Audit a coin</h2><p class="hint">Validated strategies, chart patterns, market regime, sizing — a trader brief, the live chart and the full report. About a minute per coin.</p>'
            f'<div class="row"><div><label>Coin(s), e.g. SOL or SOL ARB INJ</label><input class="big" type="text" name="coins" value="{E(coins)}" '
            f'placeholder="SOL" required maxlength="120" autocomplete="off" autocapitalize="characters"></div>'
            f'<div><label>Timeframe</label>{tf_select("tf", True, tf)}</div></div>{adv}<button>Run audit</button></form>')


def scan_form() -> str:
    return (f'<form class="card" method="post" action="/jobs"><input type="hidden" name="kind" value="scan"><h2>Scan the market</h2>'
            f'<p class="hint">Audit the most traded coins, rank them and get a correlation-aware allocation. Several minutes per coin.</p>'
            f'<div class="row row3"><div><label>Top N coins by 24h volume</label><input type="number" name="n" value="20" min="2" max="60"></div>'
            f'<div><label>Timeframe</label>{tf_select("tf", False)}</div><div><label>Processes</label><input type="number" name="jobs" value="1" min="1" max="4"></div></div>'
            f'<div class="checks">{checkbox("ml", "ML meta-labeler", False)}{checkbox("market", "Crypto-wide + macro layer", True)}'
            f'{checkbox("futures", "Futures data", True)}{checkbox("notify", "Send alert", False)}</div><button>Run scan</button></form>')


def maintenance_row() -> str:
    return ('<div class="actions"><form method="post" action="/jobs"><input type="hidden" name="kind" value="demo"><button class="secondary small">Demo check (synthetic data)</button></form>'
            '<form method="post" action="/jobs"><input type="hidden" name="kind" value="review"><button class="secondary small">Review past audits</button></form>'
            '<form method="post" action="/dashboard/rebuild"><button class="secondary small">Rebuild dashboard file</button></form></div>')


def opportunities_table(latest: list[dict], limit: int = 50) -> str:
    if not latest:
        return '<div class="empty">No audits yet — type a coin in the box above, or run the demo check.</div>'
    rows = []
    for s in latest[:limit]:
        plan = f'{_px(s.get("entry_low"))}–{_px(s.get("entry_high"))} · stop {_px(s.get("stop"))} · T1 {_px(s.get("target1"))}'
        appr = ", ".join(s.get("approved") or []) or "—"
        rows.append(f'<tr><td class="sym"><a href="/coins/{E(s["symbol"])}">{E(s["symbol"])}</a><span class="tf">{E(s["tf"])}</span>'
                    f'<div class="rel" title="{E(s["when"])} UTC">{E(rel_time(s["when"]))}</div></td>'
                    f'<td>{action_chip(s.get("action"))}<div class="rel">{E("signal: " + appr if appr != "—" else "no approved signal")}</div></td>'
                    f'<td>{verdict_badge(s.get("verdict"), s.get("score"))}</td>'
                    f'<td class="price">{_px(s.get("price"))}<div class="rel">{E(plan)}</div></td>'
                    f'<td><a href="/coins/{E(s["symbol"])}">open</a> · <a href="/reports/{E(s["md"])}">report</a></td></tr>')
    return (f'<div class="wrap"><table><thead><tr><th>Coin</th><th>What to do</th><th>Verdict · score</th><th>Last close · entry · stop · T1</th>'
            f'<th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


# ----------------------------------------------------------------------- pages
def overview_page(ctx, warnings: list[str], error: str = "", status: int = 200):
    cfg, manager, scheduler, index, wl = ctx.cfg, ctx.manager, ctx.scheduler, ctx.index, ctx.watchlist
    latest = index.latest()
    live = manager.live_counts()
    free_gb = shutil.disk_usage(cfg.data_dir).free / 1e9 if cfg.data_dir.exists() else 0
    now_ops = [s for s in latest if (s.get("action") or {}).get("code") in ("BUY_ZONE", "WAIT_PULLBACK")]
    fav = sum(1 for s in latest if s.get("verdict") == "FAVORABLE")
    appr = sum(len(s.get("approved") or []) for s in latest)
    entries = wl.entries()
    tiles = "".join(f'<div class="tile"><div class="label">{E(a)}</div><div class="value">{E(str(b))}</div><div class="delta">{E(c)}</div></div>'
                    for a, b, c in [("Opportunities now", len(now_ops), "approved signal with a live buy zone"),
                                    ("Favorable", fav, f"of {len(latest)} coins audited"),
                                    ("Approved signals", appr, "on the last closed candles"),
                                    ("Watchlist", len(entries), f"{sum(1 for e in entries if e.get('enabled', True))} re-audited on candle closes"),
                                    ("Jobs", f"{live['running']} running", f"{live['queued']} waiting"),
                                    ("Last audit", rel_time(max((s["when"] for s in latest), default="")) or "never", f"{free_gb:.1f} GB disk free")])
    tiles = tiles.replace('<div class="value">', '<div class="value sm">', 6).replace('<div class="value sm">', '<div class="value">', 4)
    banners = "".join(f'<div class="banner">{E(w)}</div>' for w in warnings)
    if error:
        banners += f'<div class="banner err">{E(error)}</div>'
    if free_gb < 0.2 and cfg.data_dir.exists():
        banners += '<div class="banner err">Less than 200 MB of disk left — scheduled runs are paused. Increase the disk size or lower the retention in Settings.</div>'
    sched = schedule_table(scheduler)
    body = f"""<h1>Overview</h1><p class="sub">What the latest audits say to do, coin by coin. Levels are reference points — this tool never trades.</p>
{banners}<div class="tiles">{tiles}</div>
<h2>What to do now</h2>{opportunities_table(latest)}
<div class="grid2" style="margin-top:18px">{audit_form()}<div class="card"><h2>Scan the market</h2><p class="hint">Rank the most traded coins by the same audit and get a correlation-aware allocation.</p>
<a class="btn secondary" href="/audit#scan">Open the scanner</a><h4>Maintenance</h4>{maintenance_row()}</div></div>
<h2>Schedule</h2>{sched}<h2>Recent jobs</h2>{jobs_table(manager.list(6))}<p><a href="/jobs">all jobs →</a></p>"""
    resp = page("Overview", body, active="home", counts=live)
    resp.status_code = status
    return resp


def schedule_table(scheduler) -> str:
    if scheduler and scheduler.schedules:
        return ('<div class="wrap tight"><table><thead><tr><th>Name</th><th>Runs</th><th>Last run (UTC)</th><th>Settings</th></tr></thead><tbody>'
                + "".join(f'<tr><td>{E(r["name"])}</td><td>{E(r["when"])}</td><td>{E(r["last_run"])}</td><td class="muted">{E(json.dumps(r["params"]))}</td></tr>'
                          for r in scheduler.status()) + "</tbody></table></div>")
    return ('<p class="muted">Nothing scheduled. Add coins to the <a href="/watchlist">watchlist</a> to re-audit them after every candle close, '
            'or set <code>COIN_AUDIT_WATCHLIST=SOL,ARB</code> in the environment.</p>')


def audit_page(ctx):
    return page("Audit", f'<h1>Audit a coin</h1><p class="sub">One coin, one timeframe, the whole pipeline: validation, patterns, regime, sizing, brief, live chart.</p>'
                f'<div class="grid2">{audit_form()}<div id="scan">{scan_form()}</div></div><h2>Maintenance</h2>{maintenance_row()}',
                active="audit", counts=ctx.manager.live_counts())


def coin_page(ctx, symbol: str):
    cfg, index, wl = ctx.cfg, ctx.index, ctx.watchlist
    audits = index.for_symbol(symbol)
    on_list = {(e["symbol"], e["tf"]) for e in wl.entries()}
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    if not audits:
        body = (f'<h1>{E(symbol)}</h1><p class="sub">No audit of this coin yet.</p>{audit_form(base)}')
        return page(symbol, body, active="reports", counts=ctx.manager.live_counts())
    latest = audits[0]
    a = load_json(cfg.reports / f"{latest['base']}.json") or {}
    b, ch = a.get("brief"), a.get("chart")
    hist = score_history(cfg.audits_csv, symbol, latest["tf"])
    spark = dashboard.sparkline([h[1] for h in hist]) if len(hist) >= 2 else ""
    rerun = (f'<form class="inline" method="post" action="/jobs"><input type="hidden" name="kind" value="audit"><input type="hidden" name="coins" value="{E(a.get("base") or base)}">'
             f'<input type="hidden" name="ml" value="on"><input type="hidden" name="market" value="on"><input type="hidden" name="futures" value="on">'
             f'{tf_select("tf", True, latest["tf"])} <button class="small">Re-audit now</button></form>')
    on = (symbol, latest["tf"]) in on_list
    wlform = (f'<form class="inline" method="post" action="/watchlist"><input type="hidden" name="symbol" value="{E(base)}"><input type="hidden" name="tf" value="{E(latest["tf"])}">'
              f'<input type="hidden" name="action" value="{"remove" if on else "add"}"><button class="secondary small">{"★ On the watchlist — remove" if on else "☆ Add to watchlist"}</button></form>')
    head = (f'<h1>{E(symbol)} <span class="muted" style="font-size:15px;font-weight:500">{E(latest["tf"])} · audited {E(latest["when"])} UTC</span></h1>'
            f'<div class="actions" style="margin:6px 0 14px">{verdict_badge(latest.get("verdict"), latest.get("score"))}{action_chip(latest.get("action"))}{spark}{rerun}{wlform}'
            f'<a class="btn secondary small" href="/reports/{E(latest["md"])}">full report</a></div>')
    chart_html = chart.embed_html(ch, live_server="/api/live") if isinstance(ch, dict) else ""
    brief_html = brief_card(b) if b else ""
    past = "".join(f'<tr><td>{E(s["when"])}</td><td>{E(s["tf"])}</td><td>{action_chip(s.get("action"))}</td><td>{verdict_badge(s.get("verdict"), s.get("score"))}</td>'
                   f'<td class="price">{_px(s.get("price"))}</td><td><a href="/reports/{E(s["md"])}">report</a></td></tr>' for s in audits[:40])
    body = (f'{head}{brief_html}{chart_html}<h2>Past audits of {E(symbol)}</h2><div class="wrap"><table><thead><tr><th>Audited (UTC)</th><th>TF</th>'
            f'<th>What to do</th><th>Verdict · score</th><th>Close</th><th></th></tr></thead><tbody>{past}</tbody></table></div>')
    return page(symbol, body, active="reports", counts=ctx.manager.live_counts())


def watchlist_page(ctx, error: str = "", status: int = 200):
    wl, index, us = ctx.watchlist, ctx.index, jobs.user_settings(ctx.cfg.user_settings_path)
    latest = {(s["symbol"], s["tf"]): s for s in index.latest()}
    rows = []
    for e in wl.entries():
        s = latest.get((e["symbol"], e["tf"]))
        base = e["symbol"][:-4] if e["symbol"].endswith("USDT") else e["symbol"]
        def f(action, label, cls="secondary"):
            return (f'<form class="inline" method="post" action="/watchlist"><input type="hidden" name="symbol" value="{E(base)}"><input type="hidden" name="tf" value="{E(e["tf"])}">'
                    f'<input type="hidden" name="action" value="{action}"><button class="{cls} small">{label}</button></form>')
        rows.append(f'<tr><td class="sym"><a href="/coins/{E(e["symbol"])}">{E(e["symbol"])}</a><span class="tf">{E(e["tf"])}</span></td>'
                    f'<td>{"<span class=ok>on</span>" if e.get("enabled", True) else "<span class=muted>paused</span>"}</td>'
                    f'<td>{action_chip(s.get("action")) if s else "<span class=muted>not audited yet</span>"}</td>'
                    f'<td>{verdict_badge(s.get("verdict"), s.get("score")) if s else ""}</td><td>{E(rel_time(s["when"])) if s else ""}</td>'
                    f'<td class="actions">{f("run", "Run now", "")}{f("disable", "Pause") if e.get("enabled", True) else f("enable", "Resume")}{f("remove", "Remove", "danger")}</td></tr>')
    table = (f'<div class="wrap"><table><thead><tr><th>Coin</th><th>Schedule</th><th>What to do</th><th>Last verdict</th><th>Audited</th><th></th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>') if rows else '<div class="empty">The watchlist is empty. Add a coin below — it is re-audited after every candle close of its timeframe.</div>'
    add = (f'<form class="card" method="post" action="/watchlist"><input type="hidden" name="action" value="add"><h2>Add a coin</h2>'
           f'<div class="row"><div><label>Coin</label><input class="big" type="text" name="symbol" placeholder="SOL" required maxlength="20" autocapitalize="characters"></div>'
           f'<div><label>Timeframe</label>{tf_select("tf", False)}</div></div><button>Add to watchlist</button></form>')
    opts = (f'<div class="card"><h2>Scheduled runs</h2><p class="hint">Applied to every watchlist audit. Alerts need a channel configured in Settings.</p>'
            f'<p>ML meta-labeler: <b>{"on" if us.get("schedule_ml") else "off"}</b> · alerts: <b>{"on" if (us.get("schedule_notify") and jobs.notify_configured()) else "off"}</b> '
            f'· <a href="/settings">change in Settings</a></p></div>')
    err = f'<div class="banner err">{E(error)}</div>' if error else ""
    resp = page("Watchlist", f'<h1>Watchlist</h1><p class="sub">Coins the scheduler re-audits after every candle close; alerts go out when a verdict qualifies.</p>{err}{table}'
                f'<div class="grid2" style="margin-top:18px">{add}{opts}</div>', active="watchlist", counts=ctx.manager.live_counts())
    resp.status_code = status
    return resp


def settings_page(ctx, message: str = "", error: str = "", status: int = 200):
    cfg = ctx.cfg
    cur = cfgmod.load(cfg.settings_toml)
    us = jobs.user_settings(cfg.user_settings_path)
    risk = cur["risk"]
    notify_on = jobs.notify_configured()
    banners = (f'<div class="banner ok">{E(message)}</div>' if message else "") + (f'<div class="banner err">{E(error)}</div>' if error else "")
    chan = ("Discord webhook" if os.environ.get("DISCORD_WEBHOOK_URL") else "") + (" · " if os.environ.get("DISCORD_WEBHOOK_URL") and os.environ.get("TELEGRAM_BOT_TOKEN") else "") + ("Telegram bot" if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID") else "")
    body = f"""<h1>Settings</h1><p class="sub">Sizing, alerts, retention and scheduled runs. Saved on the server's disk; every new audit uses them.</p>{banners}
<div class="grid2"><form class="card" method="post" action="/settings"><h2>Sizing</h2><p class="hint">Used by the trade plan on every report and by the slippage-at-your-size check.</p>
<div class="row row2"><div><label>Account size (USD)</label><input type="number" name="account_usd" value="{risk['account_usd']:g}" min="100" step="100" required></div>
<div><label>Risk per trade (% of the account)</label><input type="number" name="risk_pct" value="{risk['plan_risk_per_trade'] * 100:g}" min="0.1" max="5" step="0.1" required></div></div>
<h2 style="margin-top:16px">Alerts</h2><p class="hint">Channels come from the environment (DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID). Configured: <b>{E(chan or "none")}</b>.</p>
<label>Send an alert when the verdict is at least</label><select name="min_verdict"><option{" selected" if cur["notify"]["min_verdict"] == "FAVORABLE" else ""}>FAVORABLE</option><option{" selected" if cur["notify"]["min_verdict"] == "WATCHLIST" else ""}>WATCHLIST</option></select>
<h2 style="margin-top:16px">Scheduled runs</h2>
<div class="checks">{checkbox("schedule_ml", "Train the ML meta-labeler in scheduled audits (slower)", bool(us.get("schedule_ml")))}{checkbox("schedule_notify", "Send alerts from scheduled audits", bool(us.get("schedule_notify", True)))}</div>
<div class="row row2"><div><label>Scan the market every N hours (0 = off)</label><input type="number" name="scan_hours" value="{float(us.get('scan_hours') or 0):g}" min="0" max="168" step="1"></div>
<div><label>Coins per scan</label><input type="number" name="scan_n" value="{int(us.get('scan_n') or 20)}" min="2" max="60"></div></div>
<label>Keep reports for (days; the newest per coin is always kept)</label><input type="number" name="keep_days" value="{float(us.get('keep_days') or cfg.keep_days):g}" min="1" max="365">
<button>Save settings</button></form>
<div><div class="card"><h2>Test the alert channel</h2><p class="hint">Sends a short test message to every configured channel.</p>
<form method="post" action="/settings/test-alert"><button class="secondary"{"" if notify_on else " disabled"}>Send a test alert</button></form></div>
<div class="card" style="margin-top:16px"><h2>Where things live</h2><p class="hint">Data directory <code>{E(str(cfg.data_dir))}</code>: reports, logs, candle cache, job history, watchlist.json, user_settings.json, settings.toml (the overrides written here).</p>
<p class="hint">Login name <code>{E(cfg.user)}</code>; change the password with the COIN_AUDIT_PASSWORD environment variable.</p></div></div></div>"""
    resp = page("Settings", body, active="settings", counts=ctx.manager.live_counts())
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
    return (f"default-src 'none'; style-src 'unsafe-inline'; script-src {allowed}; connect-src {LIVE_STREAM_HOST}; img-src data:; "
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
    links.insert(0, f'<a class="btn secondary small" href="/coins/{E(a.get("symbol", ""))}">coin page</a>')
    items = [("Verdict", verdict_badge(a.get("verdict", ""), a.get("score")), ""),
             ("What to do", action_chip((a.get("brief") or {}).get("action")), ""),
             ("Last close", _px(p.get("price")), ""), ("Entry zone", f'{_px(p.get("entry_low"))} – {_px(p.get("entry_high"))}', ""),
             ("Stop", f'{_px(p.get("stop"))} ({E(str(p.get("stop_distance_pct", "")))}%)', "down"),
             ("Target 1 / 2", f'{_px(p.get("target1"))} / {_px(p.get("target2"))}', "up"),
             ("Approved signals", E(", ".join(approved)) if approved else '<span class="muted">none</span>', ""),
             ("Audited", f'{E(a.get("audit_time_utc", ""))} UTC', "")]
    summary = "".join(f'<div><div class="k">{E(k)}</div><div class="v {c}">{v}</div></div>' for k, v, c in items)
    flags = "".join(f'<div class="banner">{E(f)}</div>' for f in a.get("flags", []))
    return (f'<h1>{E(a.get("symbol", ""))} · {E(a.get("timeframe", ""))}</h1><p class="sub">{E(a.get("verdict_text", ""))}</p>'
            f'<div class="card"><div class="summary">{summary}</div><div class="actions">{"".join(links)}{rerun}</div></div>{flags}')


def reports_page(ctx) -> HTMLResponse:
    cfg = ctx.cfg
    rows = report_rows(cfg, ctx.index)
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
    body = "".join(f'<tr data-key="{E(r["symbol"].lower())} {E(r["tf"])} {E(str(r["verdict"]).lower())}"><td class="sym"><a href="/coins/{E(r["symbol"])}">{E(r["symbol"])}</a><span class="tf">{E(r["tf"])}</span></td>'
                   f'<td>{E(r["when"])}</td><td>{action_chip(r.get("action"))}</td><td>{verdict_badge(r["verdict"], r["score"]) if r["verdict"] else "<span class=muted>—</span>"}</td>'
                   f'<td class="num">{E(str(r["approved"]))}</td><td>{links(r)}</td></tr>' for r in audits[:400])
    table = (f'<input class="filter" id="filter" type="text" placeholder="filter: coin, timeframe or verdict" autocomplete="off">'
             f'<div class="wrap"><table id="reports"><thead><tr><th>Coin</th><th>Audited (UTC)</th><th>What to do</th><th>Verdict · score</th><th>Approved</th><th>Files</th></tr></thead>'
             f'<tbody>{body or "<tr><td colspan=6>No audits yet.</td></tr>"}</tbody></table></div>')
    other = ""
    if others:
        other = ('<h2>Market scans and other files</h2><div class="wrap tight"><table><thead><tr><th>File</th><th>Modified (UTC)</th><th>Files</th></tr></thead><tbody>'
                 + "".join(f'<tr><td><a href="/reports/{E(r["md"])}">{E(r["base"])}</a></td><td>{E(r["when"] or r["modified"])}</td><td>{links(r)}</td></tr>' for r in others[:100])
                 + "</tbody></table></div>")
    return page("Reports", f'<h1>Reports</h1><p class="sub">{len(audits)} audits on disk · the newest report per coin is never pruned.</p>{table}{other}',
                active="reports", counts=ctx.manager.live_counts())


# ------------------------------------------------------------------ live feed
class LiveFeed:
    """Candles for the browser's fallback polling (when Binance's stream is unreachable from the viewer's network).
    One client, answers cached for a few seconds so many open charts cost one request."""

    def __init__(self, client_factory=None, ttl: float = 3.0):
        self.factory = client_factory or (lambda: bc.BinanceClient(cache_dir=None))
        self.ttl = ttl
        self._client = None
        self._cache: dict = {}
        self._lock = __import__("threading").Lock()

    def rows(self, symbol: str, interval: str, since_ms: int) -> list[list]:
        key = (symbol, interval)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if not hit or now - hit[0] > self.ttl:
                if self._client is None:
                    self._client = self.factory()
                hit = (now, self._client.recent(symbol, interval, limit=60))
                self._cache[key] = hit
        return [r for r in hit[1] if r[0] > since_ms]


class _Ctx:
    def __init__(self, cfg, manager, scheduler, index, watchlist, live):
        self.cfg, self.manager, self.scheduler, self.index, self.watchlist, self.live = cfg, manager, scheduler, index, watchlist, live


# --------------------------------------------------------------------- app
def create_app(cfg: WebConfig | None = None, manager: jobs.JobManager | None = None, scheduler: jobs.Scheduler | None = None,
               start_background: bool = True, http_get=requests.get, live: LiveFeed | None = None,
               notify_send=None) -> FastAPI:
    cfg = cfg or WebConfig.from_env()
    if manager is None:
        manager = jobs.JobManager(cfg.data_dir, AUDIT_PY, max_concurrent=cfg.max_jobs, max_queue=cfg.max_queue)
    warnings: list[str] = []
    watchlist = jobs.Watchlist(cfg.watchlist_path)
    us0 = jobs.user_settings(cfg.user_settings_path)
    if us0.get("keep_days"):
        cfg.keep_days = float(us0["keep_days"])

    def options():
        us = jobs.user_settings(cfg.user_settings_path)
        return {"ml": bool(us.get("schedule_ml")), "notify": bool(us.get("schedule_notify", True)) and jobs.notify_configured()}

    def dynamic():
        us = jobs.user_settings(cfg.user_settings_path)
        try:
            hours = float(us.get("scan_hours") or 0)
        except (TypeError, ValueError):
            hours = 0
        if hours <= 0:
            return []
        o = options()
        return [{"name": "scan (settings)", "kind": "scan", "when": f"hours:{hours:g}",
                 "params": {"n": int(us.get("scan_n") or 20), "tf": "4h", "ml": o["ml"], "notify": o["notify"]}}]

    if scheduler is None:
        schedules, warnings = jobs.schedules_from_env()
        scheduler = jobs.Scheduler(manager, schedules, cfg.data_dir / "scheduler.json", reports_dir=cfg.reports, keep_days=cfg.keep_days,
                                   watchlist=watchlist, options=options, dynamic=dynamic)
    index = AuditIndex(cfg.reports)
    live = live or LiveFeed()
    ctx = _Ctx(cfg, manager, scheduler, index, watchlist, live)
    send_alert = notify_send or __import__("notify").send

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
    app.state.watchlist, app.state.index = watchlist, index

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
        return overview_page(ctx, warnings)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_form_page():
        return audit_page(ctx)

    @app.get("/coins/{symbol}", response_class=HTMLResponse)
    def coin(symbol: str):
        symbol = symbol.upper()
        if not COIN_PATH_RE.match(symbol):
            return secure(PlainTextResponse("Not found", status_code=404))
        if not symbol.endswith("USDT"):                                  # /coins/sol -> /coins/SOLUSDT
            return RedirectResponse(f"/coins/{symbol}USDT", status_code=302)
        return coin_page(ctx, symbol)

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
            return overview_page(ctx, warnings, error=str(e), status=400)
        return RedirectResponse(f"/jobs/{job['id']}", status_code=303)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page():
        rows = manager.list(100)
        return page("Jobs", f'<h1>Jobs</h1><p class="sub">Every audit runs as its own process; the log below each job is what it printed.</p>{jobs_table(rows)}',
                    active="jobs", counts=manager.live_counts())

    def job_view(job: dict) -> dict:
        files = report_files()
        charts = [f"{n[:-3]}_chart.html" for n in job.get("reports", []) if n.endswith(".md") and f"{n[:-3]}_chart.html" in files]
        coins = sorted({m.group(1) for n in job.get("reports", []) for m in [REPORT_NAME_RE.match(n[:-3])] if m})
        return {**job, "charts": charts, "coins": coins, "log": manager.log_tail(job["id"])}

    @app.get("/jobs/{jid}", response_class=HTMLResponse)
    def job_page(jid: str):
        job = manager.get(jid)
        if not job:
            return secure(PlainTextResponse("No such job", status_code=404))
        running = job["status"] not in jobs.TERMINAL
        v = job_view(job)
        links = "".join(f'<a class="btn small" href="/coins/{E(c)}">📈 {E(c)}</a> ' for c in v["coins"])
        links += "".join(f'<a class="btn secondary small" href="/reports/{E(n)}">📄 {E(n[:-3])}</a> ' for n in v["reports"])
        body = (f'<div id="job" data-id="{E(jid)}" data-status="{E(job["status"])}"><h1>{E(job["label"])}</h1>'
                f'<p><span id="status">{badge(job["status"])}</span> <span class="muted">created {E(job["created"])} UTC · started by {E(job["source"])}'
                f' · <span id="elapsed"></span></span></p><p class="stage" id="stage"></p>'
                f'<p id="err" class="bad">{E(job["error"])}</p><div class="actions" id="links">{links or "<span class=muted>No report yet.</span>"}</div>'
                f'<h2>Log</h2><pre class="log" id="log">{E(manager.log_tail(jid))}</pre>'
                + (f'<form method="post" action="/jobs/{E(jid)}/cancel"><button class="secondary" id="cancel">Cancel this job</button></form>' if running else "")
                + '</div>')
        return page(job["label"], body, active="jobs", counts=manager.live_counts())

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

    @app.get("/api/live/{symbol}/{tf}")
    def api_live(symbol: str, tf: str, since: int = 0):
        symbol, tf = symbol.upper(), tf.lower()
        if not SYMBOL_RE.match(symbol) or tf not in jobs.TIMEFRAMES:
            return JSONResponse({"error": "unknown symbol or timeframe"}, status_code=404)
        try:
            rows = live.rows(symbol, tf, int(since))
        except bc.BinanceError as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=502)
        except Exception as e:                                           # SDK missing, network policy, ...
            return JSONResponse({"error": f"live feed unavailable: {type(e).__name__}"}, status_code=503)
        return JSONResponse({"symbol": symbol, "interval": tf, "candles": rows, "ts": int(time.time() * 1000)})

    # ---- watchlist
    @app.get("/watchlist", response_class=HTMLResponse)
    def watchlist_get():
        return watchlist_page(ctx)

    @app.post("/watchlist")
    async def watchlist_post(request: Request):
        try:
            f = await form(request)
            action, tf = f.get("action", ""), (f.get("tf") or "4h").lower()
            sym = (f.get("symbol") or "").strip().upper()
            coin = jobs.parse_coins(sym)[0]
            full = coin if coin.endswith("USDT") else coin + "USDT"
            if action == "add":
                watchlist.add(full, tf)
            elif action == "remove":
                watchlist.remove(full, tf)
            elif action in ("enable", "disable"):
                watchlist.set_enabled(full, tf, action == "enable")
            elif action == "run":
                o = options()
                job = manager.submit("audit", {"coins": [coin], "tf": tf, "ml": o["ml"], "notify": o["notify"]}, source="web:watchlist")
                return RedirectResponse(f"/jobs/{job['id']}", status_code=303)
            else:
                raise jobs.JobError("Unknown watchlist action.")
        except (jobs.JobError, jobs.QueueFull, ValueError) as e:
            return watchlist_page(ctx, error=str(e), status=400)
        return RedirectResponse("/watchlist", status_code=303)

    # ---- settings
    @app.get("/settings", response_class=HTMLResponse)
    def settings_get(saved: int = 0):
        return settings_page(ctx, message="Settings saved. New audits use them; scheduled runs pick them up on their next tick." if saved else "")

    @app.post("/settings")
    async def settings_post(request: Request):
        try:
            f = await form(request)
            account = float(f.get("account_usd") or 0)
            risk_pct = float(f.get("risk_pct") or 0)
            keep = float(f.get("keep_days") or cfg.keep_days)
            scan_hours = float(f.get("scan_hours") or 0)
            scan_n = int(f.get("scan_n") or 20)
            mv = (f.get("min_verdict") or "FAVORABLE").upper()
            if not (100 <= account <= 1e9) or not (0.1 <= risk_pct <= 5) or not (1 <= keep <= 365) or not (0 <= scan_hours <= 168) \
                    or not (2 <= scan_n <= 60) or mv not in ("FAVORABLE", "WATCHLIST"):
                raise ValueError("A value is out of range.")
            cfgmod.write_overrides(cfg.settings_toml, {"risk": {"account_usd": account, "plan_risk_per_trade": round(risk_pct / 100, 5)},
                                                       "notify": {"min_verdict": mv}})
            jobs.save_user_settings(cfg.user_settings_path, {"keep_days": keep, "schedule_ml": "schedule_ml" in f,
                                                             "schedule_notify": "schedule_notify" in f, "scan_hours": scan_hours, "scan_n": scan_n})
            cfg.keep_days = keep
            scheduler.keep_days = keep
        except (ValueError, OSError) as e:
            return settings_page(ctx, error=f"Not saved: {e}", status=400)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/test-alert")
    def settings_test_alert():
        if not jobs.notify_configured():
            return settings_page(ctx, error="No alert channel is configured.", status=400)
        ok = send_alert("Coin Audit test alert — the channel works. (research tool, not financial advice)")
        return settings_page(ctx, message=f"Test alert delivered to: {', '.join(ok)}." if ok else "", error="" if ok else "The channel refused the message; check the webhook / token.", status=200 if ok else 502)

    # ---- connectivity
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
                    '<p class="muted">Optional sources can fail: the audit then drops that layer and says so in the report. Live charts stream straight from '
                    'Binance to your browser and fall back to this server when your own network blocks the stream.</p>', active="connectivity", counts=manager.live_counts())

    @app.get("/api/connectivity")
    def api_connectivity():
        return JSONResponse(check_connectivity(http_get))

    # ---- reports
    @app.get("/reports", response_class=HTMLResponse)
    def reports_index():
        return reports_page(ctx)

    @app.get("/reports/{name}")
    def report(name: str, raw: int = 0):
        p = cfg.reports / name
        if not NAME_RE.match(name):
            return secure(PlainTextResponse("Not found", status_code=404))
        if not p.is_file() and not (name.endswith("_chart.html") and (cfg.reports / (name[:-11] + ".json")).is_file()):
            return secure(PlainTextResponse("Not found", status_code=404))
        if name.endswith(".md") and not raw:
            files = report_files()
            a = load_json(p.with_suffix(".json")) if f"{name[:-3]}.json" in files else None
            brief_html = brief_card(a["brief"]) if a and isinstance(a.get("brief"), dict) else ""
            chart_html = chart.embed_html(a["chart"], live_server="/api/live") if a and isinstance(a.get("chart"), dict) else ""
            md = render_markdown(p.read_text(encoding="utf-8", errors="replace"))
            return page(name[:-3], f'{report_header(name, a, files)}{brief_html}{chart_html}<h2>Full report</h2><div class="md">{md}</div>',
                        active="reports", counts=manager.live_counts())
        if name.endswith(".json"):
            return secure(FileResponse(p, media_type="application/json"))
        if name.endswith("_chart.html"):
            # Prefer re-rendering from the report's JSON with today's renderer (one script, served from /static);
            # a chart page without its JSON is served as stored, allowing exactly the script it carries.
            a = load_json(cfg.reports / (name[:-11] + ".json"))
            if a and isinstance(a.get("chart"), dict):
                ch = a["chart"]
                back = f'<p class="sub"><a href="/coins/{E(ch.get("symbol", ""))}">← coin page</a> · <a href="/reports/{E(name[:-11])}.md">full report</a></p>'
                return page(f'{ch.get("symbol", "")} {ch.get("timeframe", "")} chart',
                            f'<h1>{E(ch.get("symbol", ""))} · {E(ch.get("timeframe", ""))} — {E(str(ch.get("verdict", "")))} {E(str(ch.get("score", "")))}/100</h1>'
                            f'{back}{chart.embed_html(ch, live_server="/api/live")}', active="reports", counts=manager.live_counts())
            doc = p.read_text(encoding="utf-8", errors="replace")
            return secure(HTMLResponse(doc), inline_script_csp(doc))
        if name.endswith(".html"):
            return secure(FileResponse(p, media_type="text/html"), SANDBOX_CSP if name.endswith("_tearsheet.html") else STRICT_CSP)
        return secure(FileResponse(p, media_type="text/plain; charset=utf-8"))

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page():
        body = dashboard.body(cfg.reports, cfg.audits_csv, cfg.signals_csv, link_prefix="/reports/")
        return page("Dashboard", body + '<form method="post" action="/dashboard/rebuild"><button class="secondary small">Rebuild the static dashboard.html</button></form>',
                    active="dashboard", counts=manager.live_counts())

    @app.post("/dashboard/rebuild")
    def dashboard_rebuild():
        dashboard.build(cfg.reports, cfg.audits_csv, cfg.signals_csv)
        return RedirectResponse("/dashboard", status_code=303)

    return app


app = create_app()
