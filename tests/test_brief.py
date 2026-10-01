"""The trader brief: the action it recommends, the words it uses, and that it stays consistent with the numbers."""
import json

import pytest

import brief


@pytest.fixture
def base_audit():
    price = 2.0
    return {"symbol": "SOLUSDT", "base": "SOL", "quote": "USDT", "timeframe": "4h", "verdict": "WATCHLIST", "score": 58.0,
            "flags": [], "signals": [], "strategies": [{"id": "a", "name": "A", "status": "REJECTED", "wf": {}, "benchmark": {}}],
            "patterns": {"pending_bullish": [], "fresh_bearish": [], "structure": "bullish"},
            "plan": {"price": price, "entry_low": 1.95, "entry_high": 2.0, "stop": 1.85, "target1": 2.2, "target2": 2.4,
                     "stop_distance_pct": 6.3, "position_size_pct_of_account": 15.8, "horizon": "~5.0 days (30 x 4h)",
                     "resistance_20_bars": 2.1, "resistance_90d": 2.9, "account_usd": 10000.0, "risk_fraction_used": 0.01,
                     "execution": {"position_usd": 1580.0, "units": 790.0, "position_pct_of_account": 15.8, "slippage_bps_at_size": 2.1,
                                   "exceeds_1pct_depth": False, "max_order_usd_at_backtest_slippage": 9000.0}},
            "checks": [
                {"name": "Liquidity", "score": 80, "status": "pass", "metrics": {"quote_volume_24h": 50e6, "spread_bps": 3.0, "depth_1pct_usd": 900e3}},
                {"name": "Trend (multi-timeframe)", "score": 70, "status": "pass",
                 "metrics": {"1d": {"close_gt_ema50": True, "ema50_gt_ema200": True, "di_bullish": True, "adx": 25},
                             "4h": {"close_gt_ema50": True, "ema50_gt_ema200": False, "di_bullish": True, "adx": 22}}},
                {"name": "Momentum", "score": 60, "status": "warn", "metrics": {"rsi_tf": 61.0, "roc_20_pct": 4.2, "macd_hist_tf": 0.01}},
                {"name": "Volatility & risk", "score": 90, "status": "pass", "metrics": {"atr_pct_1d": 4.1, "max_dd_90d_pct": -22.0}},
                {"name": "Market regime & relative strength", "score": 70, "status": "pass",
                 "metrics": {"btc_risk_on": True, "ret_30d_pct": 12.0, "btc_ret_30d_pct": 5.0, "ret_90d_pct": 30.0, "btc_ret_90d_pct": 10.0}},
                {"name": "Order flow & volume", "score": 62, "status": "warn", "metrics": {"taker_buy_share_7d": 0.56, "volume_7d_vs_30d": 1.3, "price_7d_pct": 6.0}},
                {"name": "Strategy library (validated)", "score": 25, "status": "fail", "metrics": {"accepted": 0, "candidates": 0, "active_approved": 0}},
                {"name": "ML meta-labeler", "score": 50, "status": "warn", "weight_mult": 0.0, "metrics": {"available": False}},
            ], "meta": {"available": False}, "market_regime": {"regime": "Broad risk-on", "behaviour": "trend modules allowed"}, "track_record": {}}


def test_watch_when_nothing_fires(base_audit):
    b = brief.build(base_audit)
    assert b["action"]["code"] == "WATCH" and b["headline"] == "SOL/USDT · 4h — WATCHLIST 58/100"
    assert "daily trend is up" in b["summary"] and "leading BTC" in b["summary"] and "Broad risk-on" in b["summary"]
    assert any("Entry zone 1.9500 – 2.0000" in x for x in b["plan"]) and any("$1,580" in x and "790" in x for x in b["plan"])
    assert any("validated edge firing" in x for x in b["would_buy"]) and any("below the stop 1.8500" in x for x in b["would_kill"])
    assert b["confidence"]["level"] == "low" and "None of the 1 strategies" in b["edge"][0]
    md = "\n".join(brief.to_markdown(b))
    assert md.startswith("## Trader brief") and "**NOTHING TO DO YET — WATCH**" in md and "**The plan**" in md
    txt = brief.to_text(b)
    assert txt.startswith("SOL/USDT") and "Would become a buy if" in txt and len(txt) <= 1800


def test_buy_zone_with_an_approved_signal(base_audit):
    a = base_audit
    a["signals"] = [{"strategy_id": "trend_ema_adx_v1", "decision": "APPROVED", "confidence": 0.6, "max_risk_fraction": 0.003}]
    a["strategies"] = [{"id": "trend_ema_adx_v1", "name": "Trend baseline", "status": "ACCEPTED", "signal_now": True,
                        "wf": {"expectancy_pct": 0.8, "trades": 61, "win_rate": 0.44, "profit_factor": 1.6}, "benchmark": {"trades_per_year": 14}}]
    a["verdict"], a["score"] = "FAVORABLE", 74.0
    b = brief.build(a)
    assert b["action"]["code"] == "BUY_ZONE" and "Buy between 1.9500 and 2.0000" in b["action"]["text"] and "1.8R" in b["action"]["text"]
    assert b["would_buy"] == [] and b["confidence"]["level"] == "medium"
    assert "+0.80% per trade" in b["edge"][0] and "FIRING NOW" in b["edge"][0]
    a["plan"]["price"] = 2.12                                                  # price ran away from the zone
    b2 = brief.build(a)
    assert b2["action"]["code"] == "WAIT_PULLBACK" and "Do not chase" in b2["action"]["text"]
    assert any("back inside 1.9500–2.0000" in x for x in b2["would_buy"])


def test_breakout_avoid_and_stale(base_audit):
    a = base_audit
    a["patterns"]["pending_bullish"] = [{"pattern": "double_bottom", "trigger_close_above": 2.05, "invalidation": 1.9, "expires_in_bars": 12}]
    b = brief.build(a)
    assert b["action"]["code"] == "WAIT_BREAKOUT" and "closing above 2.0500" in b["action"]["text"]
    assert any("close above 2.0500" in x for x in b["would_buy"]) and any("void below 1.9000" in x for x in b["would_kill"])
    a["verdict"] = "AVOID"
    assert brief.build(a)["action"]["code"] == "AVOID" and "Stay out" in brief.build(a)["action"]["text"]
    a["flags"] = ["STALE DATA: last 4h candle is 20.0h old — kill switch: no signal is valid."]
    b3 = brief.build(a)
    assert b3["action"]["code"] == "STALE" and b3["confidence"]["level"] == "none" and b3["would_buy"][0].startswith("a fresh candle")


def test_btc_risk_off_and_bearish_pattern_show_up(base_audit):
    a = base_audit
    a["checks"][4]["metrics"]["btc_risk_on"] = False
    a["patterns"]["fresh_bearish"] = [{"pattern": "head_shoulders", "bars_ago": 2, "level": 1.97}]
    a["track_record"] = {"dip_ema20_v1": {"drift": True, "live_win_rate": 0.2, "n": 10, "backtest_win_rate": 0.5}}
    b = brief.build(a)
    assert "BTC is risk-off" in b["action"]["text"] and any("BTC back above" in x for x in b["would_buy"])
    assert any("head shoulders completed 2 candles ago" in x for x in b["would_kill"]) and any("reclaiming 1.9700" in x for x in b["would_buy"])
    assert any("done worse live" in x for x in b["risk"])
    json.dumps(b)


def test_brief_is_part_of_a_real_audit(universe_dir):
    import audit as au
    import binance_client as bc
    client = bc.OfflineClient(universe_dir, as_of="2025-01-15T12:00:00Z")
    a = au.audit(client, None, "DEMO", "USDT", "4h", use_ml=False, use_market=False,
                 now=bc.pd.Timestamp("2025-01-15T12:00:00Z").to_pydatetime(), strategies=["trend_ema_adx_v1"])
    b = a["brief"]
    assert b["action"]["code"] in brief.ACTIONS and b["levels"]["stop"] == a["plan"]["stop"]
    md = au.to_markdown(a)
    assert "## Trader brief" in md and md.index("## Trader brief") < md.index("## Regime")
    import notify
    assert notify.signal_message(a).startswith("DEMO/USDT · 4h")
