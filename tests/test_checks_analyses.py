"""Market checks and research analyses on synthetic frames."""
import numpy as np
import pandas as pd
import pytest

import analyses
import binance_client as bc
import checks
import indicators as ind
import regime as rg
import synthetic as sy


@pytest.fixture(scope="module")
def frames():
    m15 = sy.gbm_ohlcv(96 * 400, "15m", seed=21)
    btc15 = sy.gbm_ohlcv(96 * 400, "15m", seed=22, price=40_000, ann_vol=0.6)
    f = {tf: ind.add_all(sy.resample(m15, tf)) for tf in ("15m", "1h", "4h", "1d")}
    f["btc_1d"] = ind.add_all(sy.resample(btc15, "1d"))
    f["eth_1d"] = ind.add_all(sy.resample(sy.gbm_ohlcv(96 * 400, "15m", seed=23, price=2500), "1d"))
    return f


def test_liquidity_scoring_and_illiquid_note():
    t = {"quoteVolume": "50000000"}
    book = bc.synthetic_book(100.0, 50_000_000, spread_bps=4.0)
    r = checks.liquidity(t, book)
    assert r["name"] == "Liquidity" and r["score"] >= 65 and r["status"] == "pass"
    assert r["metrics"]["spread_bps"] == pytest.approx(4.0, abs=0.05) and r["metrics"]["depth_1pct_usd"] > 0
    thin = checks.liquidity({"quoteVolume": "400000"}, bc.synthetic_book(1.0, 400_000, spread_bps=60.0))
    assert thin["score"] < 40 and any("Volume under" in n for n in thin["notes"])
    assert checks.liquidity(t, {"bids": [], "asks": []})["score"] == 0


def test_trend_momentum_volatility_rs_flow(frames):
    f = frames
    t = checks.trend(f["1d"], f["4h"], f["1h"])
    assert 0 <= t["score"] <= 100 and {"1d", "4h", "1h"} <= set(t["metrics"])
    m = checks.momentum(f["4h"], f["1d"], "4h")
    assert 0 <= m["score"] <= 100 and "rsi_tf" in m["metrics"]
    v = checks.volatility(f["1d"], f["1h"])
    assert 0 <= v["score"] <= 100 and "har_rv" in v["metrics"] and v["metrics"]["har_rv"]["available"]
    v2 = checks.volatility(f["1d"])
    assert "har_rv" not in v2["metrics"]
    rs = checks.relative_strength(f["1d"], f["btc_1d"], False)
    assert "btc_risk_on" in rs["metrics"] and "corr_btc_90d" in rs["metrics"]
    rs_btc = checks.relative_strength(f["btc_1d"], None, True)
    assert rs_btc["score"] in (75, 35)
    fl = checks.flow(f["15m"], f["1h"], f["1d"])
    assert 0 <= fl["score"] <= 100 and 0 < fl["metrics"]["taker_buy_share_7d"] < 1


def test_regime_classify_with_funding(frames):
    reg = rg.classify(frames["4h"], "4h", True, True, {"available": True, "mean_rate_8h": 0.0005})
    assert reg["funding"] == "crowded-long" and reg["btc"] == "risk-on"


def test_pairs_cointegration_detects_a_planted_pair(frames):
    idx = frames["1d"].index[-400:]
    rng = np.random.default_rng(3)
    x = np.exp(np.cumsum(rng.normal(0, 0.02, 400)) + 5)
    y = x * np.exp(rng.normal(0, 0.01, 400))                # y = x + stationary noise -> cointegrated
    coin = pd.DataFrame({"close": y}, index=idx)
    ref = pd.DataFrame({"close": x}, index=idx)
    out = analyses.pairs(coin, {"BTC": ref})
    row = out["rows"][0]
    assert row["coint_p_365d"] < 0.05 and row["hurst"] < 0.5 and out["status"] in {"CANDIDATE", "REJECTED"}
    assert analyses.pairs(coin.head(50), {"BTC": ref.head(50)})["status"] == "N/A"


def test_other_analyses_run(frames, universe_dir):
    f = frames
    fut = bc.OfflineFuturesClient(universe_dir)
    carry = analyses.funding_carry(fut, "DEMOUSDT")
    assert carry["available"] and carry["status"] in {"CANDIDATE", "REJECTED"} and "annualised_pct" in carry
    mm = analyses.market_making({"spread_bps": 3.0}, f["15m"])
    assert mm["status"] == "REJECTED"                          # 3 bps spread never beats 20 bps of maker fees
    rev = analyses.reversal_15m(f["15m"])
    assert rev["status"] == "EXCLUDED" and "t_stat" in rev
    reg = rg.classify(f["4h"], "4h", True, True, None)
    g = analyses.grid(f["4h"], {**reg, "trend_state": "range", "vol_state": "normal"})
    assert g["status"] in {"ENABLED", "DISABLED"} and g["levels"] >= 0
    assert analyses.grid(f["4h"], {**reg, "trend_state": "trend", "vol_state": "normal"})["status"] == "DISABLED"
    nar = analyses.narrative_phase(f["1d"], f["btc_1d"], 30e6)
    assert nar["bucket"] == "mid-cap" and nar["status"] in {"FITS", "OFF-PHASE"}
    fs = fut.funding_series("DEMOUSDT", int(f["4h"]["open_time"].iloc[0]))
    import patterns as pt
    sq = analyses.squeeze(fs, {"oi_change_24h_pct": 6.0, "long_short_ratio_accounts": 1.1}, f["4h"], pt.pivots(f["4h"])[0])
    assert sq["status"] in {"NONE", "CANDIDATE (short squeeze)", "WARNING (long squeeze risk)"}
    assert analyses.squeeze(None, {}, f["4h"], pt.pivots(f["4h"])[0])["status"] == "N/A"
    d = analyses.dca(f["1d"])
    assert d["status"] == "INFO" and d["invested"] > 0 and "return_pct" in d
