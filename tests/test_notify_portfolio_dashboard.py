import json

import numpy as np
import pandas as pd

import dashboard
import notify
import portfolio


class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_notify_channels_and_send(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notify.send("hello") == []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    calls = []

    def fake_post(url, json=None, timeout=0):
        calls.append((url, json))
        return _Resp(200)
    assert notify.send("hello", post=fake_post) == ["discord", "telegram"]
    assert calls[0][1]["content"] == "hello" and calls[1][1]["chat_id"] == "c"
    assert notify.should_notify("FAVORABLE") and not notify.should_notify("WATCHLIST")
    assert notify.should_notify("WATCHLIST", "WATCHLIST")


def test_signal_message_is_compact():
    a = {"symbol": "SOLUSDT", "timeframe": "4h", "verdict": "FAVORABLE", "score": 74.2, "audit_time_utc": "2026-01-01 00:00",
         "regime": {"trend_state": "trend", "vol_state": "normal", "btc": "risk-on"}, "market_regime": None,
         "signals": [{"strategy_id": "trend_donchian_v1", "decision": "APPROVED", "confidence": 0.61, "max_risk_fraction": 0.003}],
         "plan": {"entry_low": 100.0, "entry_high": 102.0, "stop": 95.0, "target1": 110.0, "target2": 120.0}, "flags": []}
    msg = notify.signal_message(a)
    assert "SOLUSDT 4h: FAVORABLE" in msg and "APPROVED trend_donchian_v1" in msg and len(msg) < 600


class _Client:
    def ticker_24h_all(self):
        return [{"symbol": "AAAUSDT", "quoteVolume": "50000000"}, {"symbol": "BBBUSDT", "quoteVolume": "9000000"},
                {"symbol": "CCCUSDT", "quoteVolume": "1000"}, {"symbol": "DDDUPUSDT", "quoteVolume": "90000000"},
                {"symbol": "USDCUSDT", "quoteVolume": "900000000"}, {"symbol": "EEEBTC", "quoteVolume": "90000000"}]


def test_universe_filters_and_sorts():
    assert portfolio.universe(_Client(), "USDT", n=5, min_volume=5_000_000) == ["AAAUSDT", "BBBUSDT"]
    assert portfolio.universe(_Client(), "USDT", n=1, min_volume=0) == ["AAAUSDT"]


def test_rank_correlation_allocation():
    rows = [{"symbol": "A", "approved": ["x"], "accepted": 1, "score": 60, "best_expectancy_pct": 0.5, "risk_fraction": 0.003,
             "verdict": "FAVORABLE", "stop_pct": 3.0, "rs_30d_vs_btc": 5.0},
            {"symbol": "B", "approved": [], "accepted": 2, "score": 70, "best_expectancy_pct": 0.9, "risk_fraction": 0.0,
             "verdict": "WATCHLIST", "stop_pct": 2.0, "rs_30d_vs_btc": -1.0},
            {"symbol": "C", "approved": ["y"], "accepted": 0, "score": 50, "best_expectancy_pct": 0.1, "risk_fraction": 0.0015,
             "verdict": "FAVORABLE", "stop_pct": 4.0, "rs_30d_vs_btc": 2.0}]
    ranked = portfolio.rank(rows)
    assert [r["symbol"] for r in ranked] == ["A", "C", "B"]
    ranked = portfolio.cross_section(ranked)
    assert ranked[0]["rs_percentile"] == 1.0
    idx = pd.date_range("2024-01-01", periods=120, freq="1D", tz="UTC")
    base = np.cumsum(np.random.default_rng(0).normal(0, 0.02, 120))
    closes = {"A": pd.Series(np.exp(base), idx), "B": pd.Series(np.exp(base + 0.001), idx),
              "C": pd.Series(np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.02, 120))), idx)}
    corr, syms = portfolio.correlation(closes)
    assert corr.shape == (3, 3) and corr[0, 1] > 0.9
    alloc = portfolio.allocate(ranked, corr, syms)
    assert [o["symbol"] for o in alloc] == ["A", "C"] and all(o["allocated"] for o in alloc)
    md = portfolio.to_markdown(ranked, alloc, "4h", "2026-01-01 00:00", "3 coins")
    assert "| 1 | A |" in md and "Suggested allocation" in md


def test_dashboard_builds_from_reports(tmp_path):
    rep = tmp_path / "reports"; rep.mkdir()
    a = {"symbol": "SOLUSDT", "timeframe": "4h", "audit_time_utc": "2026-01-01 00:00", "verdict": "FAVORABLE", "score": 72.0,
         "regime": {"trend_state": "trend", "vol_state": "normal", "btc": "risk-on"}, "market_regime": {"regime": "Broad risk-on", "alt_score": 75},
         "signals": [{"strategy_id": "trend_donchian_v1", "decision": "APPROVED"}], "strategies": [{"status": "ACCEPTED"}, {"status": "REJECTED"}],
         "plan": {"entry_low": 100.0, "entry_high": 102.0, "stop": 95.0, "target1": 110.0}, "flags": ["x"]}
    (rep / "SOLUSDT_4h_2026-01-01_0000.json").write_text(json.dumps(a))
    (rep / "SOLUSDT_4h_2026-01-01_0000.md").write_text("# report")
    logs = tmp_path / "logs"; logs.mkdir()
    (logs / "audits.csv").write_text("audit_time_utc,symbol,timeframe,score,verdict,outcome,outcome_return_pct\n"
                                     "2025-12-31 00:00,SOLUSDT,4h,60,NEUTRAL,target1,3.1\n2026-01-01 00:00,SOLUSDT,4h,72,FAVORABLE,,\n")
    out = dashboard.build(rep, logs / "audits.csv")
    txt = out.read_text()
    assert out.name == "dashboard.html" and "SOLUSDT" in txt and "FAVORABLE" in txt and "<svg" in txt
    assert "prefers-color-scheme: dark" in txt and "trend_donchian_v1" in txt and "100% positive" in txt
