"""Hostile validation harness shared by every strategy in the library.

Backtest model (spot, long/flat): decision on a candle's close, fill at the next
candle's open; ATR stop fixed at entry (fills at the worse of stop/open); optional
ATR take-profit and time stop; per-trade position weight (for volatility sizing);
fee + slippage charged on both sides.

Gates (from the evidence review's acceptance thresholds):
  G1 walk-forward expectancy > 0 after costs      G6 no small group of trades explains the profit
  G2 walk-forward profit factor >= 1.20            G7 final holdout (used once) expectancy > 0
  G3 positive in a majority of folds               G8 enough trades (>= 20 walk-forward)
  G4 still PF > 1 at 2x fees + slippage            G9 bootstrap significance p < 0.10
  G5 neighbouring parameters also viable           G0 lookahead self-test (signals unchanged on truncated data)
Parameters are chosen per fold on PAST data only (anchored walk-forward).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import overfit
from settings import CFG
from strategies import Strategy as _StrategyBase

Strategy_prepare = _StrategyBase.prepare

FEE = CFG["costs"]["fee"]              # Binance spot taker fee per side (0.1%, VIP0 without BNB discount)
SLIPPAGE = CFG["costs"]["slippage"]    # assumed slippage per side
COST = FEE + SLIPPAGE
FOLDS = 5
COOLDOWN_BARS = 2               # Freqtrade-style CooldownPeriod after a stop-out
STOPLOSS_GUARD = (3, 48, 24)    # StoplossGuard: 3 stops within 48 candles -> pause 24
HOLDOUT = 0.15
RNG = np.random.default_rng(11)
G = CFG["gates"]
N_GATES = 12
GATE_LEGEND = ("G0 lookahead · G1 expectancy>0 · G2 PF≥{pf} · G3 fold stability · G4 2x-cost PF>1 · "
               "G5 neighbouring params · G6 not driven by top 5% trades · G7 holdout · G8 ≥{nt} trades · "
               "G9 bootstrap p<{p} · G10 PBO<{pbo} (CSCV) · G11 deflated Sharpe≥{dsr}").format(
    pf=G["min_profit_factor"], nt=G["min_trades"], p=G["max_p_value"], pbo=G["max_pbo"], dsr=G["min_dsr"])


# ---------------------------------------------------------------- backtest
@dataclass
class Trades:
    entry_i: np.ndarray
    exit_i: np.ndarray
    ret: np.ndarray            # net return on the capital allocated to the trade * weight
    bar_ret: np.ndarray        # per-bar portfolio return (for quantstats)
    in_pos_now: bool = False
    risk: np.ndarray = None    # capital at risk per trade (weight x stop distance) -> R multiples


def backtest(df: pd.DataFrame, entry: np.ndarray, exit_: np.ndarray, stop_atr: float | None = 2.0,
             tp_atr: float | None = None, max_bars: int | None = None,
             weight: np.ndarray | None = None, cost: float = COST, *,
             stop_pct: float | None = None, tp_pct: float | None = None,
             stop_px: np.ndarray | None = None, tp_r: float | None = None, tp_px: np.ndarray | None = None,
             limit_px: np.ndarray | None = None, limit_bars: int = 3,
             trail: tuple | None = None, roi: list | None = None, early_cut: list | None = None,
             cooldown: int = COOLDOWN_BARS, guard: tuple | None = STOPLOSS_GUARD) -> Trades:
    """Long/flat spot backtest. Exit mechanics (all optional):
    stop: stop_px[signal] > stop_pct > stop_atr x ATR   ·  target: tp_r x risk > tp_pct > tp_atr x ATR
    trail=(activate_pct, trail_pct) offset-activated trailing stop (uses the previous candle's high)
    roi=[(bars_held, min_profit_pct), ...] ROI ladder  ·  max_bars time stop
    early_cut=[(bars_held, loss_in_atr), ...] cascading early-loss cuts (checked on close, exit next open)
    limit_px: limit entry at that price, valid limit_bars candles (else market at next open)
    Protections: cooldown candles after a stop-out; guard=(n_stops, window, pause) stoploss guard."""
    o, h, lo, c, a = (df[k].values for k in ("open", "high", "low", "close", "atr"))
    n = len(df)
    w = np.ones(n) if weight is None else np.nan_to_num(weight, nan=0.0)
    bar = np.zeros(n)
    ent, ext, rets, stops_at, risks = [], [], [], [], []
    cur_risk = 0.0
    pos, px, stop, tp, ei, wt, peak, atr_e = False, 0.0, 0.0, None, 0, 1.0, 0.0, 0.0
    pend = None                      # (limit, expires_i, sig_i)
    blocked_until = 0

    def close_trade(i, out, reason):
        nonlocal pos, blocked_until
        bar[i] = wt * (out / c[i - 1] * (1 - cost) - 1) if i != ei else wt * (out / px * (1 - cost) / (1 + cost) - 1)
        ent.append(ei); ext.append(i); rets.append(wt * (out / px * (1 - cost) / (1 + cost) - 1))
        risks.append(cur_risk)
        pos = False
        if reason == "stop":
            stops_at.append(i)
            blocked_until = max(blocked_until, i + cooldown)
            if guard:
                k, win, pause = guard
                if sum(1 for s in stops_at if s > i - win) >= k:
                    blocked_until = max(blocked_until, i + pause)

    def open_trade(i, fill, sig):
        nonlocal pos, px, stop, tp, ei, wt, peak, atr_e, cur_risk
        pos, px, ei, wt, peak, atr_e = True, fill, i, float(min(w[sig], 1.0)), fill, a[sig]
        if stop_px is not None and not np.isnan(stop_px[sig]):
            stop = min(stop_px[sig], px * 0.999)
        elif stop_pct:
            stop = px * (1 - stop_pct)
        else:
            stop = px - (stop_atr or 2.0) * a[sig]
        tp = (px + tp_r * (px - stop)) if tp_r else (px * (1 + tp_pct)) if tp_pct else \
            (px + tp_atr * a[sig]) if tp_atr else None
        if tp_px is not None and np.isfinite(tp_px[sig]) and tp_px[sig] > px:
            tp = tp_px[sig]
        cur_risk = wt * max((px - stop) / px, 1e-6)
        if lo[i] <= stop:                       # stopped on the entry candle
            close_trade(i, stop, "stop")
        else:
            bar[i] = wt * (c[i] / px / (1 + cost) - 1)

    for i in range(1, n):
        if pos:
            held = i - ei
            if trail and peak >= px * (1 + trail[0]):
                stop = max(stop, peak * (1 - trail[1]))
            out = reason = None
            if lo[i] <= stop:
                out, reason = min(stop, o[i]), "stop"
            elif tp is not None and h[i] >= tp:
                out, reason = max(tp, o[i]), "target"
            elif roi:
                r_need = None
                for b_, r_ in roi:
                    if held >= b_:
                        r_need = r_
                if r_need is not None and h[i] >= px * (1 + r_need):
                    out, reason = max(o[i], px * (1 + r_need)), "roi"
            if out is None:
                cut = early_cut and any(held - 1 >= b_ and c[i - 1] - px <= -l_ * atr_e for b_, l_ in early_cut)
                if (exit_[i - 1] and i - 1 > ei) or (max_bars and held >= max_bars) or cut:
                    out, reason = o[i], "exit"
            if out is not None:
                close_trade(i, out, reason)
            else:
                bar[i] = wt * (c[i] / c[i - 1] - 1)
                peak = max(peak, h[i])
        if pos:
            continue
        if pend is not None:
            L, exp_i, sig = pend
            if i > exp_i:
                pend = None
            elif lo[i] <= L:
                pend = None
                open_trade(i, min(o[i], L), sig)
                continue
        can = (entry[i - 1] and not np.isnan(a[i - 1]) and w[i - 1] > 0 and i - 1 >= blocked_until
               and (not ext or ext[-1] < i))
        if can:
            if limit_px is not None and not np.isnan(limit_px[i - 1]):
                pend = (limit_px[i - 1], i - 1 + limit_bars, i - 1)
                if lo[i] <= pend[0]:
                    L, _, sig = pend; pend = None
                    open_trade(i, min(o[i], L), sig)
            else:
                open_trade(i, o[i], i - 1)
    return Trades(np.array(ent, int), np.array(ext, int), np.array(rets), bar, pos, np.array(risks))


def stats(r: np.ndarray, risk: np.ndarray | None = None) -> dict:
    if len(r) == 0:
        return {"trades": 0, "win_rate": 0.0, "expectancy_pct": 0.0, "profit_factor": 0.0,
                "net_return_pct": 0.0, "max_dd_pct": 0.0, "avg_R": 0.0}
    eq = np.cumprod(1 + r)
    g, l = r[r > 0].sum(), -r[r < 0].sum()
    return {"trades": int(len(r)), "win_rate": round(float((r > 0).mean()), 3),
            "expectancy_pct": round(float(r.mean()) * 100, 3),
            "profit_factor": round(float(g / l), 2) if l > 0 else (99.0 if g > 0 else 0.0),
            "net_return_pct": round(float(eq[-1] - 1) * 100, 1),
            "max_dd_pct": round(float((eq / np.maximum.accumulate(eq) - 1).min()) * 100, 1),
            "avg_R": round(float(np.mean(r / risk)), 2) if risk is not None and len(risk) == len(r) else 0.0}


def _pf(r):
    g, l = r[r > 0].sum(), -r[r < 0].sum()
    return g / l if l > 0 else (99.0 if g > 0 else 0.0)


# ------------------------------------------------------------- validation
@dataclass
class Result:
    params: dict
    wf: dict
    wf_cost2x: dict
    holdout: dict
    fold_returns: list
    gates: dict
    status: str
    neighbour_share: float
    concentration: dict
    monte_carlo: dict
    lookahead_ok: bool
    benchmark: dict
    signal_now: bool
    in_position_now: bool
    trades_dev: Trades | None = None
    wf_trade_returns: np.ndarray = field(default_factory=lambda: np.array([]))
    notes: list = field(default_factory=list)
    overfit: dict = field(default_factory=dict)
    levels_now: dict = field(default_factory=dict)      # stop / target / limit price on the last candle


def _last_level(arr) -> float | None:
    try:
        v = float(arr[-1])
    except (TypeError, IndexError, ValueError):
        return None
    return v if np.isfinite(v) else None


def _grid(space: dict) -> list[dict]:
    keys = list(space)
    return [dict(zip(keys, v)) for v in itertools.product(*space.values())]


def _neighbours(p: dict, space: dict, grid: list[dict]) -> list[dict]:
    """Combos that differ from p by one step in exactly one parameter."""
    out = []
    for q in grid:
        diffs = [k for k in space if q[k] != p[k]]
        if len(diffs) == 1:
            k = diffs[0]
            vals = list(space[k])
            if abs(vals.index(q[k]) - vals.index(p[k])) == 1:
                out.append(q)
    return out


_PREP_CACHE: dict = {}


def prepared(strategy, df: pd.DataFrame, ctx: dict, cut: int) -> pd.DataFrame:
    """Indicator frame recomputed from raw candles cut at `cut`, shared by every strategy that uses the
    default prepare() (the indicator pass is the slow part of the lookahead test)."""
    own = type(strategy).prepare is not Strategy_prepare
    key = (id(df), len(df), df.index[-1], cut, type(strategy).__name__ if own else "default")
    if key not in _PREP_CACHE:
        if len(_PREP_CACHE) > 64:
            _PREP_CACHE.clear()
        _PREP_CACHE[key] = strategy.prepare(df.iloc[:cut].copy(), ctx)
    return _PREP_CACHE[key]


def lookahead_test(strategy, df: pd.DataFrame, ctx: dict, params: dict, cuts=None) -> bool:
    """Recompute signals on data cut at earlier bars; the signal at the cut bar must match."""
    full_e, full_x = strategy.signals(df, ctx, params)[:2]
    for cut in (cuts or (len(df) - 150, len(df) - 41, len(df) - 3)):
        if cut < 300:
            continue
        part = prepared(strategy, df, ctx, cut)
        e, x = strategy.signals(part, ctx, params)[:2]
        if bool(e[-1]) != bool(full_e[cut - 1]) or bool(x[-1]) != bool(full_x[cut - 1]):
            return False
    return True


def validate(strategy, df: pd.DataFrame, ctx: dict) -> Result:
    grid = _grid(strategy.space)
    n = len(df)
    hold_start = int(n * (1 - HOLDOUT))
    runs, runs2x = {}, {}
    for k, p in enumerate(grid):
        sig = strategy.signals(df, ctx, p)
        e, x, w = sig[:3]
        kw = strategy.exec_kw(p)
        kw.update(sig[3] if len(sig) > 3 else {})
        kw["weight"] = w
        runs[k] = backtest(df, e, x, **kw)
        runs2x[k] = backtest(df, e, x, cost=2 * COST, **kw)

    def sel(t: Trades, lo, hi, with_risk=False):
        m = (t.entry_i >= lo) & (t.entry_i < hi) & (t.exit_i < hi)
        return (t.ret[m], t.risk[m]) if with_risk else t.ret[m]

    # anchored walk-forward over the development period
    start = int(hold_start * 0.4)
    step = (hold_start - start) // FOLDS
    wf_r, wf_r2, fold_rets, chosen, wf_risk = [], [], [], [], []
    for f in range(FOLDS):
        lo_, hi_ = start + f * step, start + (f + 1) * step
        # choose parameters on data strictly before the fold
        best, best_score = 0, -np.inf
        for k in runs:
            r = sel(runs[k], 0, lo_)
            if len(r) >= 5:
                sc = r.mean() * np.sqrt(len(r))
                if sc > best_score:
                    best, best_score = k, sc
        chosen.append(best)
        (r, rk), r2 = sel(runs[best], lo_, hi_, True), sel(runs2x[best], lo_, hi_)
        wf_r.append(r); wf_r2.append(r2); wf_risk.append(rk)
        fold_rets.append(float(np.prod(1 + r) - 1) if len(r) else None)
    wf_all = np.concatenate(wf_r) if wf_r else np.array([])
    wf_all2 = np.concatenate(wf_r2) if wf_r2 else np.array([])
    wf_riskall = np.concatenate(wf_risk) if wf_risk else np.array([])

    # final choice = best on the whole development period; holdout scored once
    dev_score = {k: (sel(runs[k], 0, hold_start).mean() * np.sqrt(len(sel(runs[k], 0, hold_start)))
                     if len(sel(runs[k], 0, hold_start)) >= 5 else -np.inf) for k in runs}
    final = max(dev_score, key=dev_score.get)
    params = grid[final]
    hold_r, hold_risk = sel(runs[final], hold_start, n, True)

    nb = _neighbours(params, strategy.space, grid)
    nb_ok = [q for q in nb if _pf(sel(runs[grid.index(q)], 0, hold_start)) > 1.0]
    nb_share = len(nb_ok) / len(nb) if nb else 1.0

    # concentration: remove the best 5% of walk-forward trades (at least one)
    conc = {"top_trades_removed": 0, "expectancy_without_top_pct": 0.0, "top_share_of_profit": 0.0}
    if len(wf_all):
        k = max(1, int(np.ceil(0.05 * len(wf_all))))
        srt = np.sort(wf_all)
        rest = srt[:-k]
        gross = wf_all[wf_all > 0].sum()
        conc = {"top_trades_removed": k,
                "expectancy_without_top_pct": round(float(rest.mean()) * 100, 3) if len(rest) else 0.0,
                "top_share_of_profit": round(float(srt[-k:].sum() / gross), 2) if gross > 0 else 0.0}

    # Monte Carlo: bootstrap mean (significance) + shuffled-order drawdown distribution
    mc = {"p_value": 1.0, "dd_p95_pct": 0.0, "dd_median_pct": 0.0}
    if len(wf_all) >= 5:
        boots = RNG.choice(wf_all, size=(2000, len(wf_all)), replace=True).mean(axis=1)
        dds = []
        for _ in range(500):
            eq = np.cumprod(1 + RNG.permutation(wf_all))
            dds.append((eq / np.maximum.accumulate(eq) - 1).min())
        mc = {"p_value": round(float((boots <= 0).mean()), 3),
              "dd_p95_pct": round(float(np.percentile(dds, 5)) * 100, 1),
              "dd_median_pct": round(float(np.median(dds)) * 100, 1)}

    la_ok = lookahead_test(strategy, df, ctx, params)
    wf, wf2, ho = stats(wf_all, wf_riskall), stats(wf_all2), stats(hold_r, hold_risk)
    valid_folds = [x for x in fold_rets if x is not None]

    # overfitting statistics on the development window (v6): every parameter set's per-bar returns
    from regime import BARS_PER_YEAR
    bpy = BARS_PER_YEAR[ctx["tf"]]
    M = np.column_stack([runs[k].bar_ret[start:hold_start] for k in runs])
    pbo = overfit.cscv_pbo(M)
    dsr = overfit.deflated_sharpe(M[:, final], [overfit.sharpe(M[:, k]) for k in runs], bpy)
    ov = {"pbo": None if pbo is None else pbo["pbo"], "p_loss_oos": None if pbo is None else pbo["p_loss_oos"],
          "degradation_slope": None if pbo is None else pbo["degradation_slope"],
          "cscv_combos": 0 if pbo is None else pbo["combos"], "trials": len(grid), **dsr}

    gates = {
        "G0_lookahead": la_ok,
        "G1_expectancy": wf["expectancy_pct"] > 0,
        "G2_profit_factor": wf["profit_factor"] >= G["min_profit_factor"],
        "G3_fold_stability": len(valid_folds) >= 3 and sum(x > 0 for x in valid_folds) > len(valid_folds) / 2,
        "G4_cost_stress_2x": wf2["profit_factor"] > 1.0,
        "G5_param_stability": nb_share >= 0.5,
        "G6_concentration": conc["expectancy_without_top_pct"] > 0,
        "G7_holdout": ho["trades"] >= 3 and ho["expectancy_pct"] > 0,
        "G8_min_trades": wf["trades"] >= G["min_trades"],
        "G9_significance": mc["p_value"] < G["max_p_value"],
        "G10_pbo": pbo is None or pbo["pbo"] < G["max_pbo"],
        "G11_deflated_sharpe": dsr["dsr"] >= G["min_dsr"],
    }
    if all(gates.values()):
        status = "ACCEPTED"
    elif la_ok and gates["G1_expectancy"] and gates["G8_min_trades"] and sum(gates.values()) >= G["candidate_min_gates"]:
        status = "CANDIDATE"
    else:
        status = "REJECTED"

    sig_now = strategy.signals(df, ctx, params)
    e, x = sig_now[:2]
    extras = sig_now[3] if len(sig_now) > 3 and isinstance(sig_now[3], dict) else {}
    levels_now = {k: _last_level(extras.get(k)) for k in ("stop_px", "tp_px", "limit_px")}
    # benchmarks over the same walk-forward window: buy & hold, no-trade, strategy (chosen params)
    c_ = df["close"].values
    t = runs[final]
    br = t.bar_ret[start:hold_start]
    bhr = np.diff(c_[start:hold_start]) / c_[start:hold_start - 1]
    sh = lambda r: float(r.mean() / r.std() * np.sqrt(bpy)) if len(r) > 2 and r.std() > 0 else 0.0
    inpos = np.zeros(n, bool)
    for e_i, x_i in zip(t.entry_i, t.exit_i):
        inpos[e_i:x_i + 1] = True
    years = max((hold_start - start) / bpy, 1e-9)
    bench = {"bh_return_pct": round(float(c_[hold_start - 1] / c_[start] - 1) * 100, 1),
             "bh_sharpe": round(sh(bhr), 2), "no_trade_return_pct": 0.0,
             "strategy_return_pct": round(float(np.prod(1 + br) - 1) * 100, 1),
             "strategy_sharpe": round(sh(br), 2),
             "beats_bh_sharpe": sh(br) > sh(bhr),
             "trades_per_year": round(wf["trades"] / years, 1),
             "exposure_pct": round(float(inpos[start:hold_start].mean()) * 100, 1)}
    # performance by market regime at entry (development period, chosen parameters)
    cl, e2, e50 = df["close"].values, df["ema200"].values, df["ema50"].values
    slope = np.r_[np.zeros(5), e50[5:] - e50[:-5]]
    hv = (df["atr_pct"] >= df["atr_pct"].rolling(500, min_periods=100).quantile(0.8)).values
    reg = np.where((cl > e2) & (slope > 0), "bull", np.where((cl < e2) & (slope < 0), "bear", "sideways"))
    m_dev = (t.entry_i < hold_start) & (t.exit_i < hold_start)
    by_regime = {}
    for name_, mask in (("bull", reg == "bull"), ("bear", reg == "bear"), ("sideways", reg == "sideways"),
                        ("high_vol", hv)):
        mm = m_dev & mask[np.clip(t.entry_i, 0, n - 1)] if len(t.entry_i) else np.array([], bool)
        st_ = stats(t.ret[mm], t.risk[mm]) if len(t.entry_i) else stats(np.array([]))
        by_regime[name_] = {k: st_[k] for k in ("trades", "win_rate", "profit_factor", "avg_R")}
    bench["by_regime"] = by_regime
    return Result(benchmark=bench, params=params, wf=wf, wf_cost2x=wf2, holdout=ho, fold_returns=fold_rets,
                  gates=gates, status=status, neighbour_share=round(nb_share, 2),
                  concentration=conc, monte_carlo=mc, lookahead_ok=la_ok,
                  signal_now=bool(e[-1]), in_position_now=runs[final].in_pos_now,
                  trades_dev=runs[final], wf_trade_returns=wf_all, overfit=ov, levels_now=levels_now)
