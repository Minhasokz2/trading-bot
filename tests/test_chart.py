"""The chart data behind the interactive chart, the stand-alone chart page and (when a headless browser is
available) the rendering itself."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

import audit as au
import chart
import indicators as ind
import synthetic as sy

ROOT = Path(__file__).resolve().parents[1]


def fake_audit_result(df, **over) -> dict:
    price = float(df["close"].iloc[-1])
    a = {"symbol": "DEMOUSDT", "timeframe": "4h", "verdict": "WATCHLIST", "score": 61.0, "verdict_text": "Watch it.",
         "flags": ["BTC is risk-off — verdict capped at WATCHLIST."], "audit_time_utc": "2025-01-30 00:00",
         "data_until_utc": "2025-01-29 20:00", "offline": True,
         "plan": {"price": price, "entry_low": price * 0.98, "entry_high": price, "stop": price * 0.93, "target1": price * 1.09,
                  "target2": price * 1.18, "stop_distance_pct": 6.0, "position_size_pct_of_account": 16.7, "horizon": "~5 days",
                  "resistance_20_bars": price * 1.04, "resistance_90d": price * 1.9},
         "signals": [{"strategy_id": "trend_ema_adx_v1", "decision": "APPROVED", "confidence": 0.58, "max_risk_fraction": 0.005,
                      "entry_type": "market", "price": price, "stop_px": price * 0.94, "target_px": price * 1.1},
                     {"strategy_id": "dip_ema20_v1", "decision": "BLOCKED: regime is not 'range'", "confidence": 0.5,
                      "max_risk_fraction": 0.0, "entry_type": "limit", "price": price, "stop_px": price * 0.95, "target_px": price * 1.05}],
         "patterns": {"structure": "bullish", "recent_candles": [{"bars_ago": 1, "candles": ["hammer"]}],
                      "pending_bullish": [{"pattern": "double_bottom", "trigger_close_above": price * 1.02, "invalidation": price * 0.9,
                                           "expires_in_bars": 12, "start": len(df) - 40}],
                      "fresh_bearish": [{"pattern": "head_shoulders", "bars_ago": 2, "level": price * 0.97}]},
         "regime": {"trend_state": "trend", "vol_state": "normal", "btc": "risk-off"}, "market_regime": {"regime": "Alt season"}}
    a.update(over)
    return a


@pytest.fixture(scope="module")
def frame():
    return ind.add_all(sy.resample(sy.gbm_ohlcv(96 * 200, "15m", seed=3), "4h"))


def test_build_embeds_a_window_of_typed_candles(frame):
    a = fake_audit_result(frame)
    ch = chart.build(a, frame, window=300, tick=0.0001)
    assert ch["window"] == 300 and len(ch["candles"]) == 300 and ch["price_decimals"] == 4 and ch["tf_ms"] == 14_400_000
    t, o, h, l, c, v = ch["candles"][-1]
    assert t == int(frame.index[-1].value // 1_000_000) and l <= min(o, c) <= max(o, c) <= h and v > 0
    assert set(ch["ema"]) == {"ema20", "ema50", "ema200"} and all(len(s) == 300 for s in ch["ema"].values())
    assert ch["ema"]["ema20"][-1] == pytest.approx(float(frame["ema20"].iloc[-1]), rel=1e-4)
    assert all(0 <= i < 300 for i, _ in ch["pivots"]["highs"] + ch["pivots"]["lows"]) and ch["pivots"]["highs"]
    assert ch["plan"]["rr1"] == pytest.approx((1.09 - 0.99) / (0.99 - 0.93), abs=0.01) and ch["plan"]["rr2"] > ch["plan"]["rr1"]
    kinds = [p["kind"] for p in ch["patterns"]]
    assert kinds == ["bullish", "bearish"] and ch["patterns"][0]["start"] == 300 - 40
    assert [m["kind"] for m in ch["markers"]] == ["candle", "signal"] and ch["markers"][1]["approved"]
    assert ch["markers"][1]["i"] == 299 and ch["markers"][0]["i"] == 298 and ch["markers"][0]["text"] == "hammer"
    assert [s["approved"] for s in ch["signals"]] == [True, False]
    assert [lv["label"] for lv in ch["levels"]] == ["20-bar high"]              # the 90-day high is too far away to draw
    assert ch["regime"] == {"trend": "trend", "vol": "normal", "btc": "risk-off", "market": "Alt season"}
    json.dumps(ch)                                                               # plain JSON, no numpy types


def test_build_survives_missing_pieces(frame):
    a = fake_audit_result(frame, signals=[], patterns={}, regime={}, market_regime=None, flags=[])
    ch = chart.build(a, frame.head(50), window=500)
    assert ch["window"] == 50 and ch["signals"] == [] and ch["patterns"] == [] and ch["markers"] == [] and ch["regime"] is None
    nan_frame = frame.copy()
    nan_frame.loc[nan_frame.index[-1], "ema200"] = np.nan
    assert chart.build(a, nan_frame, window=10)["ema"]["ema200"][-1] is None


def test_price_decimals():
    assert chart.price_decimals(60_000.0) == 2 and chart.price_decimals(3.2) == 4 and chart.price_decimals(0.2) == 6
    assert chart.price_decimals(0.00001) == 8 and chart.price_decimals(3.2, tick=0.01) == 2 and chart.price_decimals(0.2, tick=1e-5) == 5


def test_embed_and_standalone_html(frame):
    a = fake_audit_result(frame)
    a["chart"] = chart.build(a, frame, window=100)
    a["report_path"] = "DEMOUSDT_4h_2025-01-30_0000.md"
    frag = chart.embed_html(a["chart"])
    assert 'data-coin-chart="coin-chart-data"' in frag and '<script src="/static/chart.js"></script>' in frag
    assert "</script>" not in json.loads(frag.split('">', 1)[1].split("</script>")[0])["verdict_text"]
    page = chart.standalone_html(a)
    assert page.startswith("<!doctype html>") and "<script>" in page and chart.CHART_JS in page and "DEMOUSDT_4h_2025-01-30_0000.md" in page
    digest = base64.b64encode(hashlib.sha256(chart.CHART_JS.encode()).digest()).decode()
    assert chart.CHART_JS_HASH == "sha256-" + digest                             # the CSP hash matches the inline script
    a["verdict_text"] = "<script>alert(1)</script>"
    assert "<script>alert" not in chart.embed_html(chart.build(a, frame, window=10))   # JSON is safe inside <script type=json>


def test_audit_result_carries_the_chart_and_save_writes_the_page(universe_dir, tmp_path, monkeypatch):
    import binance_client as bc
    monkeypatch.setattr(au, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(au, "LOG", tmp_path / "logs" / "audits.csv")
    monkeypatch.setattr(au, "SIGNAL_LOG", tmp_path / "logs" / "signals.csv")
    monkeypatch.setattr(au, "TRACK_RECORD", tmp_path / "logs" / "track_record.json", raising=False)
    client = bc.OfflineClient(universe_dir, as_of="2025-01-15T12:00:00Z")
    a = au.audit(client, None, "DEMO", "USDT", "4h", use_ml=False, use_market=False,
                 now=bc.pd.Timestamp("2025-01-15T12:00:00Z").to_pydatetime(), strategies=["trend_ema_adx_v1"])
    ch = a["chart"]
    assert ch["symbol"] == "DEMOUSDT" and len(ch["candles"]) == chart.WINDOW and ch["candles"][-1][0] == int(a["data_until_utc"].replace("-", "").replace(" ", "").replace(":", "")) * 0 + ch["candles"][-1][0]
    assert ch["verdict"] == a["verdict"] and ch["plan"]["stop"] == a["plan"]["stop"]
    path = au.save(a, tearsheet=False)
    chart_file = path.with_name(path.stem + "_chart.html")
    assert chart_file.exists() and a["chart_file"] == chart_file.name
    assert f"[{chart_file.name}]({chart_file.name})" in path.read_text(encoding="utf-8")
    saved = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert saved["chart"]["window"] == chart.WINDOW and saved["chart_file"] == chart_file.name


# ------------------------------------------------------------ rendering (needs Node + Playwright + Chromium)
def _playwright_available() -> bool:
    if not shutil.which("node"):
        return False
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(root) and (Path(root) / "playwright").exists()


RENDER_JS = """
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
  const problems = [];
  page.on("pageerror", e => problems.push("pageerror: " + e.message));
  page.on("console", m => { if (m.type() === "error") problems.push("console: " + m.text()); });
  await page.goto("file://" + process.argv[2], { waitUntil: "load" });
  await page.waitForTimeout(300);
  const canvas = await page.$(".cc-root canvas");
  const box = canvas ? await canvas.boundingBox() : null;
  const legend = await page.textContent(".cc-legend");
  const panel = await page.textContent(".cc-panel");
  let painted = 0;
  if (box) {
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.4);
    await page.mouse.wheel(0, -300);
    painted = await page.evaluate(() => {
      const c = document.querySelector(".cc-root canvas"), g = c.getContext("2d");
      const d = g.getImageData(0, 0, c.width, c.height).data;
      let n = 0; for (let i = 0; i < d.length; i += 4) { if (d[i] !== d[i + 1] || d[i + 1] !== d[i + 2]) n++; }   // coloured pixels
      return n;
    });
  }
  console.log(JSON.stringify({ problems, box, legend, panel, painted }));
  await browser.close();
})().catch(e => { console.log(JSON.stringify({ problems: ["crash: " + e.message] })); });
"""


@pytest.mark.skipif(not _playwright_available(), reason="needs node + the playwright module + chromium")
def test_chart_renders_in_a_real_browser(frame, tmp_path):
    a = fake_audit_result(frame)
    a["chart"] = chart.build(a, frame, window=250)
    html_path = tmp_path / "x_chart.html"
    html_path.write_text(chart.standalone_html(a), encoding="utf-8")
    script = tmp_path / "render.js"
    script.write_text(RENDER_JS)
    env = {**os.environ, "NODE_PATH": subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()}
    out = subprocess.run(["node", str(script), str(html_path)], capture_output=True, text=True, timeout=120, env=env)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["problems"] == [], res
    assert res["box"] and res["box"]["width"] > 500 and res["box"]["height"] > 250
    assert res["painted"] > 5000                                            # candles, lines and boxes were actually drawn
    assert "EMA20" in res["legend"] and "WATCHLIST" in res["panel"] and "double bottom" in res["panel"] and "APPROVED" in res["panel"]


LIVE_JS = """
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 800 } });
  await ctx.addInitScript(() => {
    class FakeWS {                                      // Binance kline stream stand-in: 6 updates, then a new candle
      constructor(url) { window.__ws = url; this.n = 0; setTimeout(() => { this.onopen && this.onopen({}); this.tick(); }, 30); }
      tick() {
        if (this.closed) return;
        const d = JSON.parse(document.getElementById("coin-chart-data").textContent), last = d.candles[d.candles.length - 1];
        this.n++;
        const t = this.n <= 6 ? last[0] + d.tf_ms : last[0] + 2 * d.tf_ms, c = last[4] * (1 + 0.01 * this.n);
        this.onmessage && this.onmessage({ data: JSON.stringify({ k: { t, o: String(last[4]), h: String(c * 1.001), l: String(last[4] * 0.999), c: String(c), v: "5", x: this.n === 6 } }) });
        if (this.n < 9) setTimeout(() => this.tick(), 40);
      }
      close() { this.closed = true; }
    }
    window.WebSocket = FakeWS;
  });
  const page = await ctx.newPage();
  const problems = [];
  page.on("pageerror", e => problems.push("pageerror: " + e.message));
  await page.goto("file://" + process.argv[2], { waitUntil: "load" });
  await page.waitForTimeout(900);
  const out = { problems, ws: await page.evaluate(() => window.__ws), status: await page.textContent(".cc-live"),
                box: (await page.textContent(".cc-livebox")).replace(/\\s+/g, " "), n: await page.evaluate(() => window.CoinChart.charts[0].n),
                ema: await page.evaluate(() => window.CoinChart.charts[0].d.ema.ema20.length) };
  console.log(JSON.stringify(out));
  await browser.close();
})().catch(e => { console.log(JSON.stringify({ problems: ["crash: " + e.message] })); });
"""


@pytest.mark.skipif(not _playwright_available(), reason="needs node + the playwright module + chromium")
def test_chart_keeps_streaming_after_the_audit(frame, tmp_path):
    """The live mode: candles arrive over Binance's kline stream (faked here), the forming candle is replaced, a new one
    appended, the EMAs extended and the plan re-read against the live price."""
    a = fake_audit_result(frame, offline=False)
    a["chart"] = chart.build(a, frame, window=120)
    assert a["chart"]["live"] == {"enabled": True, "symbol": "DEMOUSDT", "interval": "4h", "stream": "demousdt@kline_4h"}
    html_path = tmp_path / "live_chart.html"
    html_path.write_text(chart.standalone_html(a), encoding="utf-8")
    script = tmp_path / "live.js"
    script.write_text(LIVE_JS)
    env = {**os.environ, "NODE_PATH": subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()}
    out = subprocess.run(["node", str(script), str(html_path)], capture_output=True, text=True, timeout=120, env=env)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["problems"] == [], res
    assert res["ws"] == "wss://data-stream.binance.vision/ws/demousdt@kline_4h" and res["status"].strip() == "● live"
    assert res["n"] == 122 and res["ema"] == 122                               # the forming candle, then the next one
    assert "since the audit" in res["box"] and "above the entry zone" in res["box"]
    assert chart.live_descriptor({**a, "offline": True})["enabled"] is False
    assert chart.live_descriptor({**a, "point_in_time": True})["reason"].startswith("point-in-time")
