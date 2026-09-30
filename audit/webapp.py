"""Hosted web app (v6.1): run audits from a browser, watch them, read the reports. Password protected.

    python audit/serve.py          # Render / Docker start this; it also works on your own machine

Design
  * The server only orchestrates: every audit runs as a subprocess (see jobs.py), so the web process stays
    small and an out-of-memory kill in a job cannot take the site down.
  * One middleware guards every route except /healthz and /static/app.js: HTTP Basic authentication with a
    constant-time comparison, a failed-login throttle, a same-origin check on every POST (Basic credentials
    are sent by browsers automatically, so cross-site form posts must be refused) and strict security headers.
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
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import markdown as md_lib
import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

import dashboard
import jobs
from settings import DATA_DIR, __version__

AUDIT_PY = Path(__file__).resolve().with_name("audit.py")
OPEN_PATHS = {"/healthz", "/static/app.js"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
STRICT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
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
            r = http_get(url, timeout=timeout, stream=True, headers={"User-Agent": "coin-audit-bot/6.1"})
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
EXTRA_CSS = """
nav { display:flex; gap:16px; flex-wrap:wrap; margin:0 0 18px; font-size:14px; }
nav a { font-weight:600; }
form.card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px 16px 16px; margin:8px 0 16px; }
label { display:block; font-size:12px; color:var(--ink2); margin:10px 0 3px; }
input[type=text], input[type=number], select { width:100%; max-width:340px; padding:7px 9px; border:1px solid var(--border);
  border-radius:6px; background:var(--plane); color:var(--ink); font:inherit; }
.row { display:flex; gap:18px; flex-wrap:wrap; align-items:flex-end; }
.checks label { display:inline-flex; align-items:center; gap:6px; margin:10px 16px 0 0; color:var(--ink); font-size:13px; }
button { background:var(--accent); color:#fff; border:0; border-radius:6px; padding:8px 14px; font:inherit; font-weight:600; cursor:pointer; margin-top:12px; }
button.secondary { background:transparent; color:var(--ink); border:1px solid var(--border); margin-top:0; }
.badge { display:inline-block; padding:1px 9px; border-radius:10px; font-size:12px; font-weight:600; border:1px solid var(--border); }
.badge.done { color:var(--up); } .badge.failed, .badge.timeout { color:var(--critical); } .badge.running { color:var(--accent); }
pre.log { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px; overflow:auto;
  max-height:60vh; font-size:12px; white-space:pre-wrap; word-break:break-word; }
.banner { border:1px solid var(--border); border-left:4px solid var(--warning); background:var(--surface); padding:10px 14px; border-radius:6px; margin:10px 0; }
.banner.err { border-left-color:var(--critical); }
.actions { display:flex; gap:10px; flex-wrap:wrap; } .actions form { margin:0; }
.md { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:8px 20px 20px; overflow-x:auto; }
.md table { border-collapse:collapse; margin:10px 0; } .md th, .md td { border:1px solid var(--grid); padding:4px 8px; font-size:13px; }
.md code { font-size:12px; } .md pre { overflow:auto; background:var(--plane); padding:10px; border-radius:6px; }
.md blockquote { margin:6px 0; padding:2px 12px; border-left:3px solid var(--warning); color:var(--ink2); }
.muted { color:var(--ink2); }
"""
STATUS_LABEL = {"queued": "⏳ queued", "running": "▶ running", "done": "✔ done", "failed": "✖ failed",
                "cancelled": "⊘ cancelled", "timeout": "⏱ timed out"}
NAV = '<nav><a href="/">Home</a><a href="/dashboard">Dashboard</a><a href="/reports">Reports</a><a href="/jobs">Jobs</a><a href="/connectivity">Connectivity</a></nav>'
APP_JS = """(function(){
  var el = document.getElementById('job'); if (!el) return;
  var id = el.dataset.id, log = document.getElementById('log'), st = document.getElementById('status'),
      links = document.getElementById('links'), cancel = document.getElementById('cancel'), err = document.getElementById('err');
  var terminal = ['done','failed','cancelled','timeout'];
  var label = {queued:'\\u23f3 queued', running:'\\u25b6 running', done:'\\u2714 done', failed:'\\u2716 failed', cancelled:'\\u2298 cancelled', timeout:'\\u23f1 timed out'};
  function tick() {
    fetch('/api/jobs/' + id, {credentials: 'same-origin'}).then(function(r){ return r.json(); }).then(function(j) {
      var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      log.textContent = j.log; if (atBottom) log.scrollTop = log.scrollHeight;
      st.textContent = label[j.status] || j.status; st.className = 'badge ' + j.status;
      if (j.error) err.textContent = j.error;
      if (j.reports && j.reports.length) {
        links.textContent = '';
        j.reports.forEach(function(n) { var a = document.createElement('a'); a.href = '/reports/' + encodeURIComponent(n);
          a.textContent = n; links.appendChild(a); links.appendChild(document.createTextNode('  ')); });
      }
      if (terminal.indexOf(j.status) >= 0) { if (cancel) cancel.style.display = 'none'; return; }
      setTimeout(tick, 2000);
    }).catch(function() { setTimeout(tick, 4000); });
  }
  tick();
})();
"""


def page(title: str, body: str, csp: str = STRICT_CSP) -> HTMLResponse:
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<title>{E(title)} · Coin Audit</title><style>{dashboard.CSS}{EXTRA_CSS}</style></head><body>{NAV}{body}'
           f'<p class="foot">Coin Audit {E(__version__)} · read-only Binance Spot research — no orders, no API keys · not financial advice</p></body></html>')
    return secure(HTMLResponse(doc), csp)


def badge(status: str) -> str:
    return f'<span class="badge {E(status)}">{E(STATUS_LABEL.get(status, status))}</span>'


def jobs_table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="muted">No jobs yet.</p>'
    body = "".join(f'<tr><td><a href="/jobs/{E(j["id"])}">{E(j["label"])}</a></td><td>{badge(j["status"])}</td>'
                   f'<td>{E(j["created"])}</td><td class="muted">{E(j["source"])}</td></tr>' for j in rows)
    return f'<div class="wrap"><table><thead><tr><th>Job</th><th>Status</th><th>Created (UTC)</th><th>Started by</th></tr></thead><tbody>{body}</tbody></table></div>'


def checkbox(name: str, label: str, checked: bool) -> str:
    return f'<label><input type="checkbox" name="{name}"{" checked" if checked else ""}> {E(label)}</label>'


def tf_select(name: str, include_all: bool, default: str = "4h") -> str:
    opts = list(jobs.TIMEFRAMES) + (["all"] if include_all else [])
    return f'<select name="{name}">' + "".join(f'<option{" selected" if o == default else ""}>{o}</option>' for o in opts) + "</select>"


def home_page(cfg: WebConfig, manager: jobs.JobManager, scheduler, warnings: list[str], error: str = "", status: int = 200):
    rows = manager.list(8)
    reports = sorted(cfg.reports.glob("*_*_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:8] if cfg.reports.exists() else []
    live = manager.live_counts()
    free_gb = shutil.disk_usage(cfg.data_dir).free / 1e9 if cfg.data_dir.exists() else 0
    notify_on = jobs.notify_configured()
    tiles = "".join(f'<div class="tile"><div class="label">{E(a)}</div><div class="value">{E(str(b))}</div><div class="delta">{E(c)}</div></div>'
                    for a, b, c in [("Jobs", f"{live['running']} running", f"{live['queued']} waiting"),
                                    ("Reports stored", len(list(cfg.reports.glob('*_*_*.md'))) if cfg.reports.exists() else 0,
                                     f"kept {cfg.keep_days:g} days (newest per coin always)"),
                                    ("Disk free", f"{free_gb:.1f} GB", str(cfg.data_dir)),
                                    ("Alerts", "on" if notify_on else "off", "Discord / Telegram" if notify_on else "set DISCORD_WEBHOOK_URL to enable")])
    banners = "".join(f'<div class="banner">{E(w)}</div>' for w in warnings)
    if error:
        banners += f'<div class="banner err">{E(error)}</div>'
    if free_gb < 0.2 and cfg.data_dir.exists():
        banners += '<div class="banner err">Less than 200 MB of disk left — scheduled runs are paused. Increase the disk size or lower COIN_AUDIT_KEEP_DAYS.</div>'
    sched = ""
    if scheduler and scheduler.schedules:
        sched = ('<h2>Schedule</h2><div class="wrap"><table><thead><tr><th>Name</th><th>Runs</th><th>Last run (UTC)</th><th>Settings</th></tr></thead><tbody>'
                 + "".join(f'<tr><td>{E(r["name"])}</td><td>{E(r["when"])}</td><td>{E(r["last_run"])}</td><td class="muted">{E(json.dumps(r["params"]))}</td></tr>'
                           for r in scheduler.status()) + "</tbody></table></div>")
    else:
        sched = ('<h2>Schedule</h2><p class="muted">Nothing scheduled. Set <code>COIN_AUDIT_WATCHLIST=SOL,ARB</code> (and optionally '
                 '<code>COIN_AUDIT_WATCH_TF=4h</code>) in the Environment tab to audit those coins after every candle close.</p>')
    rep = ("<ul>" + "".join(f'<li><a href="/reports/{E(p.name)}">{E(p.stem)}</a></li>' for p in reports) + "</ul>") if reports else '<p class="muted">No reports yet — run the demo check or an audit.</p>'
    body = f"""<h1>Coin Audit</h1><p class="sub">Audit a Binance Spot coin: validated strategies, chart patterns, market regime, sizing. Runs on this server.</p>
{banners}<div class="tiles">{tiles}</div>
<h2>Audit a coin</h2>
<form class="card" method="post" action="/jobs"><input type="hidden" name="kind" value="audit">
<div class="row"><div><label>Coin(s), e.g. SOL or SOL ARB INJ</label><input type="text" name="coins" placeholder="SOL" required maxlength="120"></div>
<div><label>Timeframe</label>{tf_select("tf", True)}</div></div>
<div class="checks">{checkbox("ml", "ML meta-labeler", True)}{checkbox("market", "Crypto-wide + macro layer", True)}{checkbox("futures", "Futures data (funding, OI)", True)}{checkbox("tearsheet", "HTML tearsheet", False)}{checkbox("notify", "Send alert", False)}</div>
<label>Only these strategies (optional: ids, families or name fragments, comma separated)</label><input type="text" name="strategies" maxlength="200" placeholder="all 27">
<button>Run audit</button></form>
<h2>Scan the market</h2>
<form class="card" method="post" action="/jobs"><input type="hidden" name="kind" value="scan">
<div class="row"><div><label>Top N coins by 24h volume</label><input type="number" name="n" value="20" min="2" max="60"></div>
<div><label>Timeframe</label>{tf_select("tf", False)}</div><div><label>Processes</label><input type="number" name="jobs" value="1" min="1" max="4"></div></div>
<div class="checks">{checkbox("ml", "ML meta-labeler", False)}{checkbox("market", "Crypto-wide + macro layer", True)}{checkbox("futures", "Futures data", True)}{checkbox("notify", "Send alert", False)}</div>
<button>Run scan</button></form>
<h2>Maintenance</h2><div class="actions">
<form method="post" action="/jobs"><input type="hidden" name="kind" value="demo"><button class="secondary">Demo check (synthetic data)</button></form>
<form method="post" action="/jobs"><input type="hidden" name="kind" value="review"><button class="secondary">Review past audits</button></form>
<form method="post" action="/dashboard/rebuild"><button class="secondary">Rebuild dashboard</button></form></div>
{sched}<h2>Recent jobs</h2>{jobs_table(rows)}<h2>Latest reports</h2>{rep}"""
    resp = page("Home", body)
    resp.status_code = status
    return resp


def render_markdown(text: str) -> str:
    out = md_lib.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    return out.replace("<table>", '<div class="wrap"><table>').replace("</table>", "</table></div>")


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

    # ---- public
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__, "jobs": manager.live_counts()}

    @app.get("/static/app.js")
    def app_js():
        return Response(APP_JS, media_type="application/javascript")

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
        return page("Jobs", f"<h1>Jobs</h1>{jobs_table(manager.list(100))}")

    @app.get("/jobs/{jid}", response_class=HTMLResponse)
    def job_page(jid: str):
        job = manager.get(jid)
        if not job:
            return secure(PlainTextResponse("No such job", status_code=404))
        running = job["status"] not in jobs.TERMINAL
        links = " ".join(f'<a href="/reports/{E(n)}">{E(n)}</a>' for n in job["reports"])
        body = (f'<div id="job" data-id="{E(jid)}"><h1>{E(job["label"])}</h1>'
                f'<p><span id="status">{badge(job["status"])}</span> <span class="muted">created {E(job["created"])} UTC · started by {E(job["source"])}</span></p>'
                f'<p id="err" class="muted">{E(job["error"])}</p><p>Reports: <span id="links">{links or "—"}</span></p>'
                f'<pre class="log" id="log">{E(manager.log_tail(jid))}</pre>'
                + (f'<form method="post" action="/jobs/{E(jid)}/cancel"><button class="secondary" id="cancel">Cancel this job</button></form>' if running else "")
                + '</div><script src="/static/app.js"></script>')
        return page(job["label"], body)

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
        return JSONResponse({**job, "log": manager.log_tail(jid)})

    @app.get("/connectivity", response_class=HTMLResponse)
    def connectivity_page():
        rows = check_connectivity(http_get)
        required = rows[0]
        head = ('<div class="banner">Binance market data is reachable from this server. You are good to go.</div>' if required["ok"] else
                '<div class="banner err"><b>This server cannot reach Binance market data.</b> If it says HTTP 451 the provider blocks this '
                'server\'s country. A Render service cannot change region after it is created: create a new one in Frankfurt or Singapore.</div>')
        body = "".join(f'<tr><td>{E(r["name"])}</td><td>{"✔" if r["ok"] else "✖"} {E(r["verdict"])}</td><td class="num">{E("" if r["ms"] is None else str(r["ms"]) + " ms")}</td>'
                       f'<td class="muted">{E(r["purpose"])}</td></tr>' for r in rows)
        return page("Connectivity", f'<h1>Can this server reach the data sources?</h1>{head}<div class="wrap"><table><thead><tr><th>Source</th><th>Result</th><th>Time</th><th>Used for</th></tr></thead><tbody>{body}</tbody></table></div>'
                    '<p class="muted">Optional sources can fail: the audit then drops that layer and says so in the report.</p>')

    @app.get("/api/connectivity")
    def api_connectivity():
        return JSONResponse(check_connectivity(http_get))

    # ---- reports
    @app.get("/reports", response_class=HTMLResponse)
    def reports_index():
        files = sorted((p for p in cfg.reports.glob("*") if p.is_file() and NAME_RE.match(p.name)), key=lambda p: p.stat().st_mtime, reverse=True) if cfg.reports.exists() else []
        rows = "".join(f'<tr><td><a href="/reports/{E(p.name)}">{E(p.name)}</a></td><td class="num">{p.stat().st_size / 1024:,.0f} KB</td>'
                       f'<td>{time.strftime("%Y-%m-%d %H:%M", time.gmtime(p.stat().st_mtime))}</td></tr>' for p in files[:300])
        return page("Reports", f'<h1>Reports</h1><div class="wrap"><table><thead><tr><th>File</th><th>Size</th><th>Modified (UTC)</th></tr></thead><tbody>{rows or "<tr><td colspan=3>Nothing yet.</td></tr>"}</tbody></table></div>')

    @app.get("/reports/{name}")
    def report(name: str, raw: int = 0):
        p = cfg.reports / name
        if not NAME_RE.match(name) or not p.is_file():
            return secure(PlainTextResponse("Not found", status_code=404))
        if name.endswith(".md") and not raw:
            return page(name[:-3], f'<p><a href="/reports/{E(name)}?raw=1">raw markdown</a></p><div class="md">{render_markdown(p.read_text(encoding="utf-8", errors="replace"))}</div>')
        if name.endswith(".json"):
            return secure(FileResponse(p, media_type="application/json"))
        if name.endswith(".html"):
            return secure(FileResponse(p, media_type="text/html"), SANDBOX_CSP if name.endswith("_tearsheet.html") else STRICT_CSP)
        return secure(FileResponse(p, media_type="text/plain; charset=utf-8"))

    def dashboard_file() -> Path:
        out = cfg.reports / "dashboard.html"
        if not out.exists():
            dashboard.build(cfg.reports, cfg.data_dir / "logs" / "audits.csv", cfg.data_dir / "logs" / "signals.csv")
        return out

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page():
        doc = dashboard_file().read_text(encoding="utf-8")
        return secure(HTMLResponse(doc.replace("<body>", "<body><p><a href=\"/\">← Coin Audit home</a> · <a href=\"/reports\">all reports</a></p>", 1)))

    @app.post("/dashboard/rebuild")
    def dashboard_rebuild():
        dashboard.build(cfg.reports, cfg.data_dir / "logs" / "audits.csv", cfg.data_dir / "logs" / "signals.csv")
        return RedirectResponse("/dashboard", status_code=303)

    return app


app = create_app()
