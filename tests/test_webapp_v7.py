"""v7 console: overview, coin pages, the editable watchlist that drives the scheduler, settings, the live-feed API."""
import json

import pytest
from fastapi.testclient import TestClient

import jobs
import settings as cfgmod
import webapp

AUTH = ("admin", "s3cret")


class FakeLive:
    def __init__(self):
        self.calls = []

    def rows(self, symbol, interval, since):
        self.calls.append((symbol, interval, since))
        base = 1_700_000_000_000
        rows = [[base, 1.0, 1.2, 0.9, 1.1, 100.0, True], [base + 14_400_000, 1.1, 1.3, 1.0, 1.25, 80.0, False]]
        return [r for r in rows if r[0] > since]


def write_audit(reports, symbol="SOLUSDT", tf="4h", when="2026-01-02 00:00", verdict="FAVORABLE", score=72.0, code="BUY_ZONE"):
    stem = f"{symbol}_{tf}_{when.replace(' ', '_').replace(':', '')}"
    a = {"symbol": symbol, "base": symbol[:-4], "quote": "USDT", "timeframe": tf, "audit_time_utc": when, "verdict": verdict, "score": score,
         "flags": [], "signals": [{"strategy_id": "trend_ema_adx_v1", "decision": "APPROVED"}] if code == "BUY_ZONE" else [],
         "plan": {"price": 100.0, "entry_low": 98.0, "entry_high": 100.0, "stop": 94.0, "target1": 109.0, "target2": 118.0},
         "brief": {"headline": f"{symbol[:-4]}/USDT · {tf} — {verdict} {score:.0f}/100", "action": {"code": code, "label": "Buy zone is live", "tone": "good", "text": "Buy it."},
                   "summary": "Up and to the right.", "plan": ["Entry zone 98 – 100"], "would_buy": [], "would_kill": ["a close below 94"],
                   "plus": ["Trend: up"], "minus": [], "edge": ["Trend baseline — validated"], "risk": [], "confidence": {"level": "medium", "text": "ok"}},
         "chart": {"symbol": symbol, "timeframe": tf, "tf_ms": 14_400_000, "candles": [[1_700_000_000_000, 1, 1.2, 0.9, 1.1, 10]], "ema": {},
                   "pivots": {"highs": [], "lows": []}, "plan": {"price": 100.0, "entry_low": 98.0, "entry_high": 100.0, "stop": 94.0, "target1": 109.0, "target2": 118.0},
                   "patterns": [], "markers": [], "signals": [], "levels": [], "verdict": verdict, "score": score, "verdict_text": "", "flags": [],
                   "live": {"enabled": True, "symbol": symbol, "interval": tf, "stream": f"{symbol.lower()}@kline_{tf}"}},
         "chart_file": stem + "_chart.html", "report_path": stem + ".md", "verdict_text": "Go."}
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{stem}.json").write_text(json.dumps(a))
    (reports / f"{stem}.md").write_text(f"# Coin audit: {symbol} · {tf}\n\n## Trader brief\n\nBuy it.\n")
    return stem


@pytest.fixture
def web(tmp_path, fake_audit):
    cfg = webapp.WebConfig(data_dir=tmp_path, user="admin", password="s3cret")
    m = jobs.JobManager(tmp_path, fake_audit, poll=0.05, timeouts={"audit": 600})
    live = FakeLive()
    with TestClient(webapp.create_app(cfg, m, None, http_get=lambda url, **kw: None, live=live,
                                      notify_send=lambda text: ["discord"])) as c:
        c.auth = AUTH
        c.live, c.cfg, c.app_ = live, cfg, c.app
        yield c


def test_overview_lists_what_to_do(web, tmp_path):
    assert "No audits yet" in web.get("/").text
    write_audit(tmp_path / "reports")
    write_audit(tmp_path / "reports", symbol="ARBUSDT", verdict="NEUTRAL", score=45.0, code="WATCH", when="2026-01-01 00:00")
    home = web.get("/").text
    assert "Buy zone is live" in home and "SOLUSDT" in home and "ARBUSDT" in home and "Opportunities now" in home
    assert home.index("SOLUSDT") < home.index("ARBUSDT")                     # best score first
    assert 'href="/coins/SOLUSDT"' in home and "Audit a coin" in home and "Scan the market" in home


def test_coin_page_has_brief_chart_and_history(web, tmp_path):
    write_audit(tmp_path / "reports", when="2026-01-01 00:00", verdict="NEUTRAL", score=50, code="WATCH")
    write_audit(tmp_path / "reports")
    logs = tmp_path / "logs"; logs.mkdir(exist_ok=True)
    (logs / "audits.csv").write_text("audit_time_utc,symbol,timeframe,score,verdict\n2026-01-01 00:00,SOLUSDT,4h,50,NEUTRAL\n2026-01-02 00:00,SOLUSDT,4h,72,FAVORABLE\n")
    r = web.get("/coins/SOLUSDT")
    assert r.status_code == 200 and "Buy it." in r.text and "Up and to the right." in r.text and "<svg" in r.text
    assert 'data-live-server="/api/live"' in r.text and "coin-chart-data" in r.text and "Past audits of SOLUSDT" in r.text
    assert "Add to watchlist" in r.text and "Re-audit now" in r.text
    assert web.get("/coins/sol").status_code in (200, 302) and web.get("/coins/NOPEUSDT").text.count("No audit of this coin yet") == 1
    assert web.get("/coins/../x").status_code in (404, 400)


def test_watchlist_drives_the_scheduler(web, tmp_path):
    r = web.post("/watchlist", data={"action": "add", "symbol": "sol", "tf": "4h"}, follow_redirects=False)
    assert r.status_code == 303
    web.post("/watchlist", data={"action": "add", "symbol": "ARB", "tf": "4h"})
    web.post("/watchlist", data={"action": "add", "symbol": "ETH", "tf": "1h"})
    pg = web.get("/watchlist").text
    assert "SOLUSDT" in pg and "ARBUSDT" in pg and "ETHUSDT" in pg and "not audited yet" in pg
    sched = web.app_.state.scheduler.schedules
    names = {s["name"]: s for s in sched}
    assert names["watchlist:1h"]["params"]["coins"] == ["ETHUSDT"] and names["watchlist:4h"]["params"]["coins"] == ["SOLUSDT", "ARBUSDT"]
    assert names["watchlist:4h"]["when"] == "candle:4h" and names["watchlist:4h"]["params"]["notify"] is False   # no channel configured
    web.post("/watchlist", data={"action": "disable", "symbol": "ARB", "tf": "4h"})
    assert {s["name"]: s for s in web.app_.state.scheduler.schedules}["watchlist:4h"]["params"]["coins"] == ["SOLUSDT"]
    assert "paused" in web.get("/watchlist").text
    web.post("/watchlist", data={"action": "remove", "symbol": "ETH", "tf": "1h"})
    assert "watchlist:1h" not in {s["name"] for s in web.app_.state.scheduler.schedules}
    run = web.post("/watchlist", data={"action": "run", "symbol": "SOL", "tf": "4h"}, follow_redirects=False)
    assert run.status_code == 303 and run.headers["location"].startswith("/jobs/")
    bad = web.post("/watchlist", data={"action": "add", "symbol": "bad$", "tf": "4h"})
    assert bad.status_code == 400 and "Not a valid coin symbol" in bad.text
    assert json.loads((tmp_path / "watchlist.json").read_text())["entries"][0]["symbol"] == "SOLUSDT"
    assert "4h candle close" in web.get("/").text                                 # the overview shows the schedule


def test_watchlist_is_seeded_from_the_environment(tmp_path):
    wl = jobs.Watchlist(tmp_path / "watchlist.json", env={"COIN_AUDIT_WATCHLIST": "sol, arb", "COIN_AUDIT_WATCH_TF": "1h"})
    assert [(e["symbol"], e["tf"]) for e in wl.entries()] == [("SOLUSDT", "1h"), ("ARBUSDT", "1h")]
    again = jobs.Watchlist(tmp_path / "watchlist.json", env={"COIN_AUDIT_WATCHLIST": "btc"})   # the file wins once it exists
    assert len(again.entries()) == 2
    sc = jobs.Scheduler(jobs.JobManager(tmp_path, "x.py"), [{"name": "review", "kind": "review", "when": "hours:24", "params": {}}],
                        tmp_path / "s.json", watchlist=wl, options=lambda: {"ml": True, "notify": False})
    assert [s["name"] for s in sc.schedules] == ["review", "watchlist:1h"] and sc.schedules[1]["params"]["ml"] is True


def test_settings_are_saved_and_used_by_new_audits(web, tmp_path):
    pg = web.get("/settings").text
    assert 'name="account_usd" value="10000"' in pg and "Send a test alert" in pg
    r = web.post("/settings", data={"account_usd": "25000", "risk_pct": "0.5", "min_verdict": "WATCHLIST", "keep_days": "30",
                                    "scan_hours": "12", "scan_n": "15", "schedule_ml": "on"}, follow_redirects=False)
    assert r.status_code == 303
    cfg = cfgmod.load(tmp_path / "settings.toml")
    assert cfg["risk"]["account_usd"] == 25000.0 and cfg["risk"]["plan_risk_per_trade"] == 0.005 and cfg["notify"]["min_verdict"] == "WATCHLIST"
    assert cfg["gates"]["min_trades"] == 20                                      # untouched keys keep their defaults
    us = json.loads((tmp_path / "user_settings.json").read_text())
    assert us["keep_days"] == 30 and us["schedule_ml"] is True and us["scan_hours"] == 12 and us["scan_n"] == 15
    assert web.cfg.keep_days == 30 and web.app_.state.scheduler.keep_days == 30
    scan = [s for s in web.app_.state.scheduler.schedules if s["kind"] == "scan"]
    assert scan and scan[0]["when"] == "hours:12" and scan[0]["params"]["n"] == 15 and scan[0]["params"]["ml"] is True
    assert "Settings saved" in web.get("/settings?saved=1").text and 'value="25000"' in web.get("/settings").text
    assert web.post("/settings", data={"account_usd": "5", "risk_pct": "0.5", "min_verdict": "FAVORABLE"}).status_code == 400


def test_test_alert_button(web, monkeypatch):
    assert web.post("/settings/test-alert").status_code == 400                    # nothing configured
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/hook")
    r = web.post("/settings/test-alert")
    assert r.status_code == 200 and "delivered to: discord" in r.text


def test_live_feed_api(web):
    r = web.get("/api/live/SOLUSDT/4h?since=0")
    assert r.status_code == 200 and len(r.json()["candles"]) == 2 and r.json()["candles"][1][6] is False
    assert len(web.get("/api/live/solusdt/4h?since=1700000000000").json()["candles"]) == 1
    assert web.get("/api/live/SOL/4h").status_code == 404 and web.get("/api/live/SOLUSDT/2h").status_code == 404
    assert web.live.calls[0][0] == "SOLUSDT"


def test_live_feed_caches_and_reports_errors(tmp_path):
    import binance_client as bc

    class Client:
        n = 0

        def recent(self, symbol, interval, since_ms=None, limit=60):
            Client.n += 1
            if symbol == "DEADUSDT":
                raise bc.BinanceError("Invalid symbol")
            return [[1, 1, 1, 1, 1, 1, True]]
    feed = webapp.LiveFeed(client_factory=Client, ttl=60)
    assert feed.rows("SOLUSDT", "4h", 0) == [[1, 1, 1, 1, 1, 1, True]] and feed.rows("SOLUSDT", "4h", 0) and Client.n == 1
    with pytest.raises(bc.BinanceError):
        feed.rows("DEADUSDT", "4h", 0)
    cfg = webapp.WebConfig(data_dir=tmp_path, password="pw")
    m = jobs.JobManager(tmp_path, "x.py")
    with TestClient(webapp.create_app(cfg, m, None, start_background=False, live=feed)) as c:
        c.auth = ("admin", "pw")
        assert c.get("/api/live/DEADUSDT/4h").status_code == 502
        page = c.get("/")
        assert "wss://data-stream.binance.vision" in page.headers["content-security-policy"]


def test_report_page_shows_brief_then_chart_then_report(web, tmp_path):
    stem = write_audit(tmp_path / "reports")
    r = web.get(f"/reports/{stem}.md")
    t = r.text
    assert t.index("Buy it.") < t.index("coin-chart-data") < t.index("Full report") and 'data-live-server="/api/live"' in t
    assert "coin page" in t and "/coins/SOLUSDT" in t
    chart_page = web.get(f"/reports/{stem}_chart.html")
    assert chart_page.status_code == 200 and 'data-live-server="/api/live"' in chart_page.text


def test_job_api_lists_coins(web, tmp_path):
    r = web.post("/jobs", data={"kind": "audit", "coins": "sol", "tf": "4h"}, follow_redirects=False)
    jid = r.headers["location"].rsplit("/", 1)[1]
    import time
    for _ in range(200):
        j = web.get(f"/api/jobs/{jid}").json()
        if j["status"] in jobs.TERMINAL:
            break
        time.sleep(0.1)
    assert j["status"] == "done" and j["coins"] == ["SOLUSDT"]
    assert '/coins/SOLUSDT' in web.get(f"/jobs/{jid}").text
