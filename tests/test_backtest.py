"""Backtester mechanics on a hand-built frame: fills at the next open, stop/target/time exits,
costs, protections, limit entries, trailing stops, ROI ladders and early cuts."""
import numpy as np
import pytest

import validation as val

C = val.COST
RT = (1 - C) / (1 + C)          # round-trip cost multiplier on a flat exit


def _ret(out, px=100.0, w=1.0):
    return w * (out / px * RT - 1)


def _signals(n, entries=(), exits=()):
    e, x = np.zeros(n, bool), np.zeros(n, bool)
    e[list(entries)] = True
    x[list(exits)] = True
    return e, x


def test_fill_at_next_open_and_exit_on_signal(flat_frame):
    e, x = _signals(len(flat_frame), entries=[10], exits=[15])
    t = val.backtest(flat_frame, e, x, stop_atr=2.0)
    assert t.entry_i.tolist() == [11] and t.exit_i.tolist() == [16]
    assert t.ret[0] == pytest.approx(_ret(100.0))
    assert t.ret[0] < 0                                       # a flat round trip costs 2 x (fee + slippage)


def test_stop_fills_at_stop_or_worse_open(flat_frame):
    df = flat_frame.copy()
    df.iloc[13, df.columns.get_loc("low")] = 97.0           # pierces the 98 stop (2 ATR)
    e, x = _signals(len(df), entries=[10])
    t = val.backtest(df, e, x, stop_atr=2.0)
    assert t.exit_i.tolist() == [13]
    assert t.ret[0] == pytest.approx(_ret(98.0))
    df.iloc[13, df.columns.get_loc("open")] = 96.0          # gap through the stop -> filled at the open
    t = val.backtest(df, e, x, stop_atr=2.0)
    assert t.ret[0] == pytest.approx(_ret(96.0))


def test_target_time_stop_and_weight(flat_frame):
    df = flat_frame.copy()
    df.iloc[14, df.columns.get_loc("high")] = 104.0
    e, x = _signals(len(df), entries=[10])
    t = val.backtest(df, e, x, stop_atr=2.0, tp_atr=3.0)
    assert t.exit_i.tolist() == [14] and t.ret[0] == pytest.approx(_ret(103.0))
    t = val.backtest(flat_frame, e, x, stop_atr=2.0, max_bars=3)
    assert t.exit_i.tolist() == [14]                          # filled at 11, held 3 bars -> out at open of 14
    w = np.full(len(df), 0.5)
    t = val.backtest(flat_frame, e, x, stop_atr=2.0, max_bars=3, weight=w)
    assert t.ret[0] == pytest.approx(_ret(100.0, w=0.5))
    assert t.risk[0] == pytest.approx(0.5 * 2.0 / 100.0)


def test_double_cost_hurts_more(flat_frame):
    e, x = _signals(len(flat_frame), entries=[10], exits=[15])
    a = val.backtest(flat_frame, e, x).ret[0]
    b = val.backtest(flat_frame, e, x, cost=2 * C).ret[0]
    assert b < a < 0


def test_cooldown_and_stoploss_guard(flat_frame):
    df = flat_frame.copy()
    n = len(df)
    for i in (13, 17, 21):
        df.iloc[i, df.columns.get_loc("low")] = 90.0        # every trade gets stopped
    e, x = _signals(n, entries=range(9, 40))                 # signal on every bar
    t = val.backtest(df, e, x, stop_atr=2.0, cooldown=2, guard=(3, 48, 24))
    # 1st fill 10; stop at 13 -> cooldown blocks signals 13,14 -> next fill 16 (signal 15); stop 17 -> fill 20; stop 21
    assert t.entry_i[:3].tolist() == [10, 16, 20]
    assert t.exit_i[:3].tolist() == [13, 17, 21]
    # third stop within 48 bars -> pause 24 bars: no entry before bar 21 + 24
    assert len(t.entry_i) == 3 or t.entry_i[3] >= 21 + 24


def test_limit_entry_only_fills_when_touched(flat_frame):
    df = flat_frame.copy()
    n = len(df)
    lim = np.full(n, np.nan)
    lim[10] = 99.0
    e, x = _signals(n, entries=[10])
    t = val.backtest(df, e, x, stop_atr=2.0, limit_px=lim, limit_bars=3, max_bars=5)
    assert len(t.entry_i) == 0                               # never traded down to 99 (lows are 99.5)
    df.iloc[12, df.columns.get_loc("low")] = 98.8
    t = val.backtest(df, e, x, stop_atr=2.0, limit_px=lim, limit_bars=3, max_bars=5)
    assert t.entry_i.tolist() == [12]
    assert t.ret[0] == pytest.approx(100.0 / 99.0 * RT - 1)    # filled at the limit, exits flat at 100


def test_trailing_roi_and_early_cut(flat_frame):
    df = flat_frame.copy()
    n = len(df)
    e, x = _signals(n, entries=[10])
    # trailing: rally to 105 on bar 12 -> stop moves to 105*0.99 = 103.95 from bar 13 on;
    # bar 13 holds above it, bar 14 opens 104.2 and dips to 103 -> stopped at 103.95
    df.iloc[12, df.columns.get_loc("high")] = 105.0
    df.iloc[13, [df.columns.get_loc(k) for k in ("open", "high", "low", "close")]] = [104.0, 104.5, 104.0, 104.2]
    df.iloc[14, [df.columns.get_loc(k) for k in ("open", "high", "low", "close")]] = [104.2, 104.3, 103.0, 103.5]
    t = val.backtest(df, e, x, stop_atr=2.0, trail=(0.02, 0.01))
    assert t.exit_i.tolist() == [14] and t.ret[0] == pytest.approx(_ret(103.95))
    # without the trail the same path is not stopped (2 ATR stop = 98)
    t = val.backtest(df, e, x, stop_atr=2.0)
    assert len(t.exit_i) == 0 and t.in_pos_now
    # roi ladder: after 2 bars, +1% is enough
    df = flat_frame.copy()
    df.iloc[13, df.columns.get_loc("high")] = 101.5
    t = val.backtest(df, e, x, stop_atr=5.0, roi=[(2, 0.01)])
    assert t.exit_i.tolist() == [13] and t.ret[0] == pytest.approx(_ret(101.0))
    # early cut: 3 bars in and still 1 ATR under water on the close -> out next open
    df = flat_frame.copy()
    for i in (12, 13, 14, 15):
        df.iloc[i, df.columns.get_loc("close")] = 98.9
    t = val.backtest(df, e, x, stop_atr=5.0, early_cut=[(3, 1.0)])
    assert t.exit_i.tolist() == [15]


def test_stats_and_profit_factor():
    r = np.array([0.02, -0.01, 0.03, -0.02, 0.01])
    s = val.stats(r, np.full(5, 0.02))
    assert s["trades"] == 5 and s["win_rate"] == 0.6
    assert s["profit_factor"] == pytest.approx(0.06 / 0.03, abs=0.01)
    assert s["avg_R"] == pytest.approx(np.mean(r / 0.02), abs=0.01)
    assert val.stats(np.array([]))["trades"] == 0


def test_bar_returns_compound_to_trade_returns(flat_frame):
    df = flat_frame.copy()
    df.iloc[12, df.columns.get_loc("close")] = 102.0
    df.iloc[13, df.columns.get_loc("close")] = 101.0
    e, x = _signals(len(df), entries=[10], exits=[13])
    t = val.backtest(df, e, x, stop_atr=5.0)
    eq = np.prod(1 + t.bar_ret[11:15])
    assert eq - 1 == pytest.approx(t.ret[0], abs=1e-9)
