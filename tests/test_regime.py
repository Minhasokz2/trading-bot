"""Higher-timeframe alignment never leaks a bar before it closes; regime classification is sane."""
import numpy as np
import pandas as pd

import regime as rg


def test_align_uses_only_closed_higher_timeframe_bars():
    d1 = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC"))
    h4 = pd.date_range("2024-01-01", periods=18, freq="4h", tz="UTC")
    a = rg.align(h4, "4h", d1, "1d", ["close"])["close"]
    # the 1d bar of Jan 1 closes at Jan 2 00:00 = close of the 4h bar opening Jan 1 20:00
    assert np.isnan(a.loc["2024-01-01 16:00"]) and a.loc["2024-01-01 20:00"] == 1.0
    assert a.loc["2024-01-02 16:00"] == 1.0 and a.loc["2024-01-02 20:00"] == 2.0
    assert a.loc["2024-01-03 20:00"] == 3.0


def test_align_events_are_known_at_settlement():
    base = pd.date_range("2024-01-01", periods=12, freq="1h", tz="UTC")
    times = pd.DatetimeIndex([pd.Timestamp("2024-01-01 08:00", tz="UTC")])
    s = rg.align_events(base, "1h", times, np.array([0.5]))
    assert np.isnan(s.loc["2024-01-01 06:00"]) and s.loc["2024-01-01 07:00"] == 0.5   # 07:00 bar closes at 08:00


def test_classify_and_matches(h4_frame):
    reg = rg.classify(h4_frame, "4h", btc_risk_on=True, liquidity_ok=True, funding=None)
    assert reg["trend_state"] in {"trend", "range", "transition"} and reg["vol_state"] in {"low", "normal", "high"}
    assert 0 <= reg["vol_percentile_1y"] <= 1
    assert rg.matches("any", reg) and not rg.matches("trend", {**reg, "liquidity": "thin"})
    r2 = {**reg, "trend_state": "range", "vol_state": "normal", "btc": "risk-on"}
    assert rg.matches("range", r2) and not rg.matches("trend", r2)
    assert not rg.matches("dip", {**r2, "vol_state": "high"})


def test_markov_filter_identifies_the_high_variance_state():
    rng = np.random.default_rng(1)
    idx = pd.date_range("2024-01-01", periods=630, freq="1D", tz="UTC")
    burst_end = pd.Series(np.r_[rng.normal(0, 0.01, 600), rng.normal(0, 0.05, 30)], index=idx)
    burst_start = pd.Series(np.r_[rng.normal(0, 0.05, 30), rng.normal(0, 0.01, 600)], index=idx)
    a, b = rg.markov_vol_regime(burst_end), rg.markov_vol_regime(burst_start)
    assert a["available"] and a["p_high_vol"] > 0.9 and a["high_state_vol_ratio"] > 2
    assert b["available"] and b["p_high_vol"] < 0.1
    assert not rg.markov_vol_regime(burst_end.head(50))["available"]


def test_har_rv_forecast_tracks_volatility(h4_frame):
    import synthetic as sy
    h1 = sy.resample(sy.gbm_ohlcv(96 * 200, "15m", seed=9), "1h")
    har = rg.har_rv_forecast(h1["close"], 24)
    assert har["available"] and har["days"] > 150
    assert 0 < har["forecast_daily_vol_pct"] < 50 and len(har["betas"]) == 4
    assert not rg.har_rv_forecast(h1["close"].head(24 * 20), 24)["available"]


def test_classify_reports_markov_and_high_vol_veto(h4_frame):
    reg = rg.classify(h4_frame, "4h", True, True, None)
    assert "markov" in reg and isinstance(reg["markov_high_vol"], bool)
    forced = {**reg, "vol_state": "normal", "markov_high_vol": True, "btc": "risk-on", "trend_state": "range"}
    assert not rg.matches("dip", forced) and not rg.matches("range", forced) and rg.matches("any", forced)
