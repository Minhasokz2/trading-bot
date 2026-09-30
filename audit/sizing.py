"""Position sizing and execution realism (v6).

* kelly            Kelly stake from a strategy's own walk-forward trade returns, made conservative with a
                   bootstrap quantile, then scaled by a fraction (default half-Kelly) and capped by the
                   strategy's status cap (ACCEPTED 0.3% / CANDIDATE 0.15% account risk per trade).
* impact_cost      Slippage of a taker order that walks a linear ±1% order book: spread/2 + 50 bps x
                   (order / one-sided depth). Says whether the backtest's slippage assumption holds at
                   YOUR size, and the largest order that still fits it.
* vol_target       Volatility-targeted position scaling.
* portfolio_heat   Caps total open risk across positions and per correlation cluster.
"""
from __future__ import annotations

import numpy as np

from settings import CFG

R = CFG["risk"]


def kelly(trade_returns, quantile: float | None = None, fraction: float | None = None, n_boot: int = 500,
          seed: int = 11) -> dict:
    """trade_returns are net returns on the capital allocated to each trade (weight = 1).
    Returns the full-Kelly capital fraction, its conservative bootstrap quantile and the suggested
    fraction (quantile x fraction, capped at 1 = no leverage on spot)."""
    r = np.asarray(trade_returns, float)
    r = r[np.isfinite(r)]
    quantile = R["kelly_quantile"] if quantile is None else quantile
    fraction = R["kelly_fraction"] if fraction is None else fraction
    if len(r) < 10 or r.min() <= -1:
        return {"n": int(len(r)), "full_kelly": 0.0, "kelly_conservative": 0.0, "suggested_capital_fraction": 0.0,
                "growth_rate_at_suggested": 0.0}
    grid = np.linspace(0.0, 3.0, 301)

    def best(x):
        g = np.array([np.mean(np.log1p(f * x)) for f in grid])
        return float(grid[int(np.argmax(g))]), float(g.max())

    f_full, _ = best(r)
    rng = np.random.default_rng(seed)
    boots = np.array([best(rng.choice(r, len(r), replace=True))[0] for _ in range(n_boot)])
    f_q = float(np.quantile(boots, quantile))
    sug = float(np.clip(f_q * fraction, 0.0, 1.0))
    growth = float(np.mean(np.log1p(sug * r))) if sug > 0 else 0.0
    return {"n": int(len(r)), "full_kelly": round(f_full, 3), "kelly_conservative": round(f_q, 3),
            "suggested_capital_fraction": round(sug, 3), "growth_rate_at_suggested": round(growth, 5),
            "bootstrap_quantile": quantile, "fraction_of_kelly": fraction}


def risk_fraction(kelly_capital_fraction: float, stop_pct: float, cap: float) -> float:
    """Account risk per trade implied by a capital fraction and a stop distance, capped by status."""
    return float(min(cap, max(0.0, kelly_capital_fraction) * max(stop_pct, 0.0)))


def impact_cost(order_usd: float, side_depth_usd: float, spread_bps: float) -> dict:
    """Taker slippage (bps, one side) walking a linear book whose one-sided ±1% depth is side_depth_usd."""
    if order_usd <= 0:
        return {"slippage_bps": round(spread_bps / 2, 2), "book_fraction": 0.0, "exceeds_1pct_depth": False}
    q = order_usd / max(side_depth_usd, 1.0)
    return {"slippage_bps": round(spread_bps / 2 + 50.0 * q, 2), "book_fraction": round(q, 4),
            "exceeds_1pct_depth": bool(q > 1.0)}


def max_order_for_slippage(side_depth_usd: float, spread_bps: float, target_bps: float) -> float:
    """Largest taker order (USD) whose expected slippage stays within target_bps."""
    room = target_bps - spread_bps / 2
    return float(max(0.0, room / 50.0 * side_depth_usd)) if room > 0 else 0.0


def size_plan(account_usd: float, risk_frac: float, stop_pct: float, price: float, side_depth_usd: float,
              spread_bps: float, assumed_slippage: float, fee: float) -> dict:
    """Turn a risk fraction into a position and check the execution cost at that size."""
    if stop_pct <= 0 or price <= 0:
        return {"position_usd": 0.0, "units": 0.0}
    position_usd = account_usd * risk_frac / stop_pct
    position_usd = min(position_usd, account_usd)                       # spot: no leverage
    imp = impact_cost(position_usd, side_depth_usd, spread_bps)
    slip = imp["slippage_bps"] / 1e4
    ok = slip <= assumed_slippage * 1.5
    return {"position_usd": round(position_usd, 2), "units": round(position_usd / price, 8),
            "position_pct_of_account": round(position_usd / account_usd * 100, 2),
            "slippage_bps_at_size": imp["slippage_bps"], "book_fraction": imp["book_fraction"],
            "exceeds_1pct_depth": imp["exceeds_1pct_depth"],
            "round_trip_cost_pct": round((2 * fee + 2 * slip) * 100, 3),
            "backtest_slippage_holds": ok,
            "max_order_usd_at_backtest_slippage": round(max_order_for_slippage(side_depth_usd, spread_bps,
                                                                              assumed_slippage * 1e4), 2)}


def vol_target(atr_pct: float, target_daily_vol: float = 0.02, bars_per_day: float = 6.0) -> float:
    """Fraction of full size so that the position's expected daily move equals target_daily_vol."""
    if atr_pct <= 0:
        return 1.0
    daily = atr_pct * np.sqrt(bars_per_day)
    return float(np.clip(target_daily_vol / daily, 0.1, 1.0))


def portfolio_heat(candidates: list[dict], clusters: dict[str, int] | None = None, cap: float | None = None,
                   max_positions: int | None = None, max_per_cluster: int | None = None) -> list[dict]:
    """Greedy allocation in rank order: each candidate {symbol, risk_fraction, ...} is accepted while the
    total risk stays under `cap`, the position count under `max_positions` and the count in its
    correlation cluster under `max_per_cluster`. Returns the candidates with an `allocated` flag and
    the (possibly scaled) `allocated_risk`."""
    cap = R["portfolio_heat"] if cap is None else cap
    max_positions = R["max_positions"] if max_positions is None else max_positions
    max_per_cluster = R["max_per_cluster"] if max_per_cluster is None else max_per_cluster
    clusters = clusters or {}
    used, n, per = 0.0, 0, {}
    out = []
    for c in candidates:
        rf = float(c.get("risk_fraction", 0.0))
        cl = clusters.get(c["symbol"], c["symbol"])
        room = cap - used
        take = rf > 0 and n < max_positions and per.get(cl, 0) < max_per_cluster and room > 1e-9
        alloc = min(rf, room) if take else 0.0
        if take:
            used += alloc; n += 1; per[cl] = per.get(cl, 0) + 1
        out.append({**c, "cluster": cl, "allocated": bool(take), "allocated_risk": round(alloc, 5),
                    "reason": "" if take else ("no risk budget" if rf <= 0 else "portfolio heat cap" if room <= 1e-9
                                               else "max positions" if n >= max_positions else "cluster cap")})
    return out


def clusters_from_correlation(corr, symbols: list[str], threshold: float | None = None) -> dict[str, int]:
    """Greedy single-linkage clustering: coins whose return correlation exceeds the threshold share a cluster."""
    threshold = R["cluster_corr"] if threshold is None else threshold
    corr = np.asarray(corr, float)
    n = len(symbols)
    labels = {}
    nxt = 0
    for i in range(n):
        if symbols[i] in labels:
            continue
        labels[symbols[i]] = nxt
        stack = [i]
        while stack:
            k = stack.pop()
            for j in range(n):
                if symbols[j] not in labels and np.isfinite(corr[k, j]) and corr[k, j] >= threshold:
                    labels[symbols[j]] = nxt
                    stack.append(j)
        nxt += 1
    return labels
