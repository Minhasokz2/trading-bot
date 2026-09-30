"""Planted textbook patterns must be detected and confirmed; the same shapes without breakout
volume must be rejected. Bearish shapes are research events that block longs."""
import itertools

import numpy as np
import pytest

import indicators as ind
import patterns as pt
import strategies as st
import synthetic as sy
import validation as val

STRAT = {"double_bottom": "double_triple_bottom_v1", "triple_bottom": "double_triple_bottom_v1",
         "inverse_head_shoulders": "inverse_head_shoulders_v1", "bull_flag": "bull_flag_pennant_v1",
         "high_tight_flag": "bull_flag_pennant_v1", "ascending_triangle": "triangle_wedge_breakout_v1",
         "falling_wedge": "triangle_wedge_breakout_v1", "rounding_bottom": "rounding_cup_handle_v1",
         "cup_and_handle": "rounding_cup_handle_v1", "v_bottom": "v_bottom_reclaim_v1",
         "range_breakout": "range_breakout_v1", "trendline_break": "trendline_break_retest_v1"}
BY_ID = {s.id: s for s in st.LIBRARY}


def _grid(space):
    keys = list(space)
    return [dict(zip(keys, v)) for v in itertools.product(*space.values())]


def _planted(kind, seed, with_volume):
    raw = sy.gbm_ohlcv(1500, "4h", seed=10 + seed, regimes=False, ann_vol=0.6)
    info = sy.plant(raw, kind, 900, sy.atr_scale(raw), seed=seed, with_volume=with_volume)
    return ind.add_all(raw), info


@pytest.mark.parametrize("kind", sy.BULLISH)
@pytest.mark.parametrize("seed", [0, 1])
def test_bullish_pattern_fires_with_volume_only(kind, seed):
    s = BY_ID[STRAT[kind]]
    for with_volume in (True, False):
        df, info = _planted(kind, seed, with_volume)
        st._CACHE.clear()
        fired = [p for p in _grid(s.space) if s.signals(df, {"tf": "4h", "is_btc": False}, p)[0][info["breakout"]]]
        assert bool(fired) == with_volume, (kind, seed, with_volume, fired)


@pytest.mark.parametrize("kind", sy.BEARISH)
def test_bearish_pattern_is_research_event_and_long_blocker(kind):
    df, info = _planted(kind, 0, True)
    hi, lo = pt.pivots(df)
    sets = (pt.double_bottom(df, hi, lo, side="short") + pt.head_shoulders(df, hi, lo, side="short")
            + [x for x in pt.converging(df, hi, lo) if x.get("side") == "short"])
    _, ev = pt.trigger(df, sets, vol_mult=1.2)
    assert any(x["t"] == info["breakout"] for x in ev)
    cut = df.iloc[:info["breakout"] + 2]                      # scan as of one bar after the breakdown
    scan = pt.scan(cut, val.COST)
    assert any(x["pattern"] == kind for x in scan["fresh_bearish"])
    assert scan["bearish_research"] and all("hit_rate" in r for r in scan["bearish_research"])


def test_pending_formation_reported_before_breakout():
    df, info = _planted("double_bottom", 0, True)
    cut = df.iloc[:info["breakout"] - 1]                      # the neckline has not been broken yet
    scan = pt.scan(cut, val.COST)
    names = [x["pattern"] for x in scan["pending_bullish"]]
    assert "double_bottom" in names
    row = next(x for x in scan["pending_bullish"] if x["pattern"] == "double_bottom")
    assert row["trigger_close_above"] > row["invalidation"] and row["expires_in_bars"] > 0


def test_pivots_are_confirmed_only_k_bars_later(h4_frame):
    hi, lo = pt.pivots(h4_frame)
    assert hi.max() <= len(h4_frame) - 1 - pt.K and lo.max() <= len(h4_frame) - 1 - pt.K
    # every pivot high is the unique maximum of its 2K+1 window
    H = h4_frame["high"].values
    for i in hi[:50]:
        w = H[i - pt.K:i + pt.K + 1]
        assert H[i] == w.max() and (w == H[i]).sum() == 1


def test_candlesticks_detect_textbook_shapes(flat_frame):
    df = flat_frame.copy()
    o, h, l, c = (df.columns.get_loc(k) for k in ("open", "high", "low", "close"))
    # bar 20: bearish, bar 21: bullish engulfing
    df.iloc[20, [o, h, l, c]] = [100.0, 100.2, 99.0, 99.2]
    df.iloc[21, [o, h, l, c]] = [99.1, 101.0, 98.9, 100.8]
    # bar 30: hammer in a downtrend (closes below its 10-bar mean)
    for i in range(22, 31):
        df.iloc[i, [o, h, l, c]] = [95.0 - 0.1 * i, 95.2 - 0.1 * i, 94.8 - 0.1 * i, 94.9 - 0.1 * i]
    df.iloc[30, [o, h, l, c]] = [91.5, 91.95, 89.0, 91.9]      # small body, long lower wick, tiny upper wick
    # bar 40: doji
    df.iloc[40, [o, h, l, c]] = [100.0, 101.0, 99.0, 100.01]
    cd = pt.candles(df)
    assert cd["bullish_engulfing"][21] and cd["hammer"][30] and cd["doji"][40]
    assert not cd["bullish_engulfing"][20]
    assert len(cd) == 21 and set(pt.BULL_CANDLES + pt.BEAR_CANDLES + pt.NEUTRAL_CANDLES) == set(cd)


def test_structure_labels(h4_frame):
    hi, lo = pt.pivots(h4_frame)
    assert pt.structure(h4_frame, hi, lo, len(h4_frame) - 1) in {"bullish", "bearish", "mixed", "unknown"}
