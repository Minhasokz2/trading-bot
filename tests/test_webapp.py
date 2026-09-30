"""The hosted web app: authentication, throttling, CSRF, path safety, jobs, reports, connectivity."""
import time

import pytest
import requests
from fastapi.testclient import TestClient

import jobs
import webapp

AUTH = ("admin", "s3cret")


class Resp:
    def __init__(self, code):
        self.status_code = code

    def close(self):
        pass


def fake_get(url, **kw):
    if "binance.vision" in url:
        return Resp(200)
    if "fapi" in url:
        return Resp(451)
    if "coingecko" in url:
        raise requests.ConnectionError("no route to host")
    return Resp(200)


def make_client(tmp_path, fake_audit, **cfg_kw):
    cfg = webapp.WebConfig(data_dir=tmp_path, **({"user": "admin", "password": "s3cret"} | cfg_kw))
    m = jobs.JobManager(tmp_path, fake_audit, poll=0.05, timeouts={"audit": 600})
    sc = jobs.Scheduler(m, [], tmp_path / "scheduler.json", reports_dir=cfg.reports)
    return TestClient(webapp.create_app(cfg, m, sc, http_get=fake_get))


@pytest.fixture
def web(tmp_path, fake_audit):
    with make_client(tmp_path, fake_audit) as c:
        c.auth = AUTH
        yield c


def wait_done(c, jid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in jobs.TERMINAL:
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# ------------------------------------------------------------ authentication
def test_login_required_and_security_headers(tmp_path, fake_audit):
    with make_client(tmp_path, fake_audit) as c:
        r = c.get("/")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert c.get("/", auth=("admin", "wrong")).status_code == 401
        assert c.get("/", auth=("root", "s3cret")).status_code == 401
        ok = c.get("/", auth=AUTH)
        assert ok.status_code == 200 and "Audit a coin" in ok.text and "Scan the market" in ok.text
        h = ok.headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["cache-control"] == "no-store"
        assert "script-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
        for path in ("/jobs", "/reports", "/dashboard", "/connectivity", "/api/jobs", "/api/connectivity", "/reports/x.md"):
            assert c.get(path).status_code == 401, path
        assert c.post("/jobs", data={"kind": "review"}).status_code == 401
        health = c.get("/healthz")                                            # the only open page besides the script
        assert health.status_code == 200 and health.json()["ok"] is True and c.get("/static/app.js").status_code == 200


def test_no_password_locks_everything_but_health(tmp_path, fake_audit):
    with make_client(tmp_path, fake_audit, password="") as c:
        r = c.get("/", auth=AUTH)
        assert r.status_code == 503 and "COIN_AUDIT_PASSWORD" in r.text
        assert c.get("/healthz").status_code == 200
    with make_client(tmp_path, fake_audit, password="", allow_anonymous=True) as c:
        assert c.get("/").status_code == 200


def test_failed_logins_are_throttled(tmp_path, fake_audit):
    with make_client(tmp_path, fake_audit) as c:
        for _ in range(8):
            assert c.get("/", auth=("admin", "nope")).status_code == 401
        blocked = c.get("/", auth=AUTH)                                       # even the right password waits
        assert blocked.status_code == 429 and blocked.headers["retry-after"] == "600"
        assert c.get("/healthz").status_code == 200


def test_throttle_window_and_proxy_address():
    now = [0.0]
    t = webapp.Throttle(limit=2, window=10, clock=lambda: now[0])
    t.fail("a"); t.fail("a")
    assert t.blocked("a") and not t.blocked("b")
    now[0] = 11
    assert not t.blocked("a")
    t.fail("a"); t.ok("a")
    assert not t.blocked("a") and "a" not in t.fails and "never-seen" not in t.fails
    assert not t.blocked("never-seen") and "never-seen" not in t.fails          # asking does not create an entry
    small = webapp.Throttle(limit=2, window=10, clock=lambda: now[0], max_tracked=3)
    for i in range(10):
        small.fail(f"ip{i}")
    assert len(small.fails) == 3

    class Req:
        headers = {"x-forwarded-for": "6.6.6.6, 10.1.1.1"}
        client = type("C", (), {"host": "10.0.0.9"})()

    class RenderReq(Req):
        headers = {"x-forwarded-for": "6.6.6.6, 172.70.1.1", "true-client-ip": "203.0.113.7"}
    assert webapp.client_ip(Req, True) == "10.1.1.1"                          # last entry: added by the trusted proxy
    assert webapp.client_ip(Req, False) == "10.0.0.9"                         # proxy headers are ignored unless trusted
    assert webapp.client_ip(RenderReq, True) == "203.0.113.7"                 # Render/Cloudflare: the real client, not the edge
    assert webapp.client_ip(RenderReq, False) == "10.0.0.9"
    assert webapp.client_ip(RenderReq, True, "x-real-ip") == "172.70.1.1"     # header absent -> falls back to X-Forwarded-For


# --------------------------------------------------------------------- CSRF
def test_cross_site_posts_are_refused(web):
    evil = web.post("/jobs", data={"kind": "review"}, headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert evil.status_code == 403 and "Cross-site" in evil.text
    assert web.post("/jobs", data={"kind": "review"}, headers={"origin": "null"}, follow_redirects=False).status_code == 403
    own = web.post("/jobs", data={"kind": "review"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert own.status_code == 303
    assert web.post("/jobs", data={"kind": "review"}, follow_redirects=False).status_code == 303   # curl sends no Origin
    fwd = web.post("/jobs", data={"kind": "review"}, headers={"origin": "https://my.example.com", "x-forwarded-host": "my.example.com"},
                   follow_redirects=False)
    assert fwd.status_code == 303                                             # behind a proxy the public host matches


def test_real_browser_form_posts_are_accepted(web):
    """Regression: a browser posting a form from our own page sends Sec-Fetch-Site: same-origin, and 'Origin: null'
    if the page was served with Referrer-Policy: no-referrer (which we used to send). That must not be refused."""
    same = {"sec-fetch-site": "same-origin", "sec-fetch-mode": "navigate", "origin": "null"}
    assert web.post("/jobs", data={"kind": "review"}, headers=same, follow_redirects=False).status_code == 303
    assert web.post("/jobs", data={"kind": "review"}, headers={"sec-fetch-site": "same-origin", "origin": "https://anything.example"},
                    follow_redirects=False).status_code == 303                # the browser's own verdict decides
    assert web.post("/jobs", data={"kind": "review"}, headers={"sec-fetch-site": "none"}, follow_redirects=False).status_code == 303
    for site in ("cross-site", "same-site"):
        r = web.post("/jobs", data={"kind": "review"}, headers={"sec-fetch-site": site, "origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 403, site
    # the page itself must not make browsers send Origin: null
    assert web.get("/").headers["referrer-policy"] == "same-origin"


# ---------------------------------------------------------------------- jobs
def test_submit_audit_and_read_results(web):
    r = web.post("/jobs", data={"kind": "audit", "coins": "sol", "tf": "4h", "market": "on", "futures": "on"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    jid = r.headers["location"].rsplit("/", 1)[1]
    job = wait_done(web, jid)
    assert job["status"] == "done" and job["reports"] == ["SOLUSDT_4h_2026-01-01_0000.md"]
    assert "--no-ml" in job["args"] and "--no-tearsheet" in job["args"] and "--no-market" not in job["args"]   # unchecked boxes = off
    assert "fake audit SOL --tf 4h" in job["log"]
    page = web.get(f"/jobs/{jid}")
    assert page.status_code == 200 and "Audit SOL" in page.text and "SOLUSDT_4h_2026-01-01_0000.md" in page.text
    assert 'src="/static/app.js"' in page.text and "Cancel this job" not in page.text            # finished: no cancel button
    assert jid in web.get("/jobs").text and jid in web.get("/").text
    assert web.get("/jobs/20260101-000000-000000").status_code == 404 and web.get("/api/jobs/nope").status_code == 404


def test_checkboxes_map_to_flags(web):
    r = web.post("/jobs", data={"kind": "audit", "coins": "ETH", "tf": "1h", "ml": "on", "tearsheet": "on", "market": "on"}, follow_redirects=False)
    job = wait_done(web, r.headers["location"].rsplit("/", 1)[1])
    assert "--no-ml" not in job["args"] and "--no-tearsheet" not in job["args"] and "--no-futures" in job["args"]


def test_invalid_requests_show_a_clear_error(web):
    for data, needle in [({"kind": "audit", "coins": "SO L;$"}, "valid coin"), ({"kind": "audit", "coins": ""}, "at least one coin"),
                         ({"kind": "audit", "coins": "SOL", "tf": "3m"}, "Timeframe"), ({"kind": "scan", "n": "9999", "tf": "4h"}, "between 2 and 60"),
                         ({"kind": "hack"}, "Unknown job type"), ({"kind": "audit", "coins": "SOL", "notify": "on"}, "Alerts are not configured")]:
        r = web.post("/jobs", data=data, follow_redirects=False)
        assert r.status_code == 400 and needle in r.text, (data, r.text[:300])
    assert web.post("/jobs", content=b"x" * 30_000, headers={"content-type": "application/x-www-form-urlencoded"},
                    follow_redirects=False).status_code == 400
    assert web.get("/api/jobs").json() == []                                                      # nothing was queued


def test_cancel_via_the_web(tmp_path, fake_audit):
    with make_client(tmp_path, fake_audit) as c:
        c.auth = AUTH
        r = c.post("/jobs", data={"kind": "audit", "coins": "SOL", "strategies": "sleep"}, follow_redirects=False)
        jid = r.headers["location"].rsplit("/", 1)[1]
        for _ in range(100):
            if c.get(f"/api/jobs/{jid}").json()["status"] == "running":
                break
            time.sleep(0.05)
        assert "Cancel this job" in c.get(f"/jobs/{jid}").text
        assert c.post(f"/jobs/{jid}/cancel", follow_redirects=False).status_code == 303
        assert wait_done(c, jid)["status"] == "cancelled"


def test_queue_full_message(tmp_path, fake_audit):
    cfg = webapp.WebConfig(data_dir=tmp_path, password="s3cret", max_queue=1)
    m = jobs.JobManager(tmp_path, fake_audit, poll=0.05, max_queue=1, timeouts={"audit": 600})
    with TestClient(webapp.create_app(cfg, m, jobs.Scheduler(m, [], tmp_path / "s.json"))) as c:
        c.auth = AUTH
        body = {"kind": "audit", "coins": "SOL", "strategies": "sleep"}
        first = c.post("/jobs", data=body, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
        for _ in range(100):
            if c.get(f"/api/jobs/{first}").json()["status"] == "running":
                break
            time.sleep(0.05)
        assert c.post("/jobs", data=body, follow_redirects=False).status_code == 303
        r = c.post("/jobs", data=body, follow_redirects=False)
        assert r.status_code == 400 and "already waiting" in r.text


# ------------------------------------------------------------------- reports
def test_report_viewer_and_path_safety(web, tmp_path):
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "reports" / "SOLUSDT_4h_2026-01-01_0000.md").write_text("# Coin audit\n\n> ⚠ note <script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    (tmp_path / "reports" / "SOLUSDT_4h_2026-01-01_0000.json").write_text('{"ok": true}')
    (tmp_path / "reports" / "SOLUSDT_4h_2026-01-01_0000_tearsheet.html").write_text("<html>tearsheet</html>")
    (tmp_path / "secret.txt").write_text("top secret")
    r = web.get("/reports/SOLUSDT_4h_2026-01-01_0000.md")
    assert r.status_code == 200 and "<h1>Coin audit</h1>" in r.text and "<table>" in r.text and 'class="wrap"' in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]                    # injected script cannot run
    assert web.get("/reports/SOLUSDT_4h_2026-01-01_0000.md?raw=1").text.startswith("# Coin audit")
    j = web.get("/reports/SOLUSDT_4h_2026-01-01_0000.json")
    assert j.status_code == 200 and j.json() == {"ok": True}
    ts = web.get("/reports/SOLUSDT_4h_2026-01-01_0000_tearsheet.html")
    assert ts.status_code == 200 and ts.headers["content-security-policy"].startswith("sandbox allow-scripts")
    for bad in ("../secret.txt", "..%2Fsecret.txt", "%2e%2e/secret.txt", "..%5Csecret.txt", "a/b.md", ".env", "nope.md"):
        assert web.get("/reports/" + bad).status_code in (404, 400), bad
    idx = web.get("/reports")
    assert "SOLUSDT_4h_2026-01-01_0000.md" in idx.text and "secret.txt" not in idx.text


def test_dashboard_page_and_rebuild(web):
    r = web.get("/dashboard")
    assert r.status_code == 200 and "Coin audit dashboard" in r.text and "Coin Audit home" in r.text
    assert web.post("/dashboard/rebuild", follow_redirects=False).headers["location"] == "/dashboard"


# ------------------------------------------------------------- connectivity
def test_connectivity_report(web):
    rows = web.get("/api/connectivity").json()
    by = {r["name"]: r for r in rows}
    assert by["Binance Spot market data"]["ok"] and by["Binance USD-M futures"]["status"] == 451
    assert "BLOCKED" in by["Binance USD-M futures"]["verdict"] and by["CoinGecko"]["status"] is None and "unreachable" in by["CoinGecko"]["verdict"]
    page = web.get("/connectivity")
    assert page.status_code == 200 and "reachable from this server" in page.text and "HTTP 451" in page.text


def test_connectivity_warns_when_binance_is_blocked(tmp_path, fake_audit):
    cfg = webapp.WebConfig(data_dir=tmp_path, password="s3cret")
    m = jobs.JobManager(tmp_path, fake_audit)
    blocked = lambda url, **kw: Resp(451)                                                     # noqa: E731
    with TestClient(webapp.create_app(cfg, m, jobs.Scheduler(m, [], tmp_path / "s.json"), start_background=False, http_get=blocked)) as c:
        c.auth = AUTH
        page = c.get("/connectivity").text
        assert "cannot reach Binance market data" in page and "Frankfurt or Singapore" in page


def test_home_shows_schedule_and_warnings(tmp_path, fake_audit):
    cfg = webapp.WebConfig(data_dir=tmp_path, password="s3cret")
    m = jobs.JobManager(tmp_path, fake_audit)
    sc = jobs.Scheduler(m, [{"name": "watchlist", "kind": "audit", "when": "candle:4h", "params": {"coins": ["SOL"], "tf": "4h"}}],
                        tmp_path / "s.json")
    with TestClient(webapp.create_app(cfg, m, sc, start_background=False)) as c:
        c.auth = AUTH
        home = c.get("/").text
        assert "4h candle close" in home and "watchlist" in home and "never" in home
    with make_client(tmp_path, fake_audit) as c:
        c.auth = AUTH
        assert "Nothing scheduled" in c.get("/").text and c.get("/healthz").json()["jobs"] == {"queued": 0, "running": 0}


def test_webconfig_from_env(tmp_path):
    cfg = webapp.WebConfig.from_env({"COIN_AUDIT_DATA_DIR": str(tmp_path), "COIN_AUDIT_PASSWORD": "pw", "COIN_AUDIT_USER": "me",
                                     "COIN_AUDIT_TRUST_PROXY": "1", "COIN_AUDIT_MAX_JOBS": "2", "COIN_AUDIT_KEEP_DAYS": "3",
                                     "COIN_AUDIT_CLIENT_IP_HEADER": "CF-Connecting-IP",
                                     "COIN_AUDIT_ALLOWED_ORIGINS": "https://a.example, https://b.example"})
    assert cfg.data_dir == tmp_path and cfg.user == "me" and cfg.password == "pw" and cfg.trust_proxy and cfg.max_jobs == 2
    assert cfg.client_ip_header == "cf-connecting-ip" and webapp.WebConfig.from_env({}).client_ip_header == "true-client-ip"
    assert cfg.keep_days == 3 and cfg.allowed_origins == {"https://a.example", "https://b.example"} and cfg.reports == tmp_path / "reports"
    assert webapp.WebConfig.from_env({}).password == "" and not webapp.WebConfig.from_env({}).allow_anonymous
