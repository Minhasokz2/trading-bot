"""Research analyses that are NOT spot long/flat backtests. Each returns a status and
plain-language notes; none of them trades. Their execution needs futures/margin or
tick-level infrastructure that this project deliberately does not enable yet."""
from __future__ import annotations

import numpy as np
import pandas as pd

from validation import FEE, SLIPPAGE

MAKER_FEE = 0.001        # Binance spot VIP0 maker fee (0.075% with BNB) — set to YOUR tier
FUT_TAKER = 0.0005       # USD-M futures VIP0 taker


# ------------------------------------------------------------ pairs trading
def _half_life(spread: pd.Series) -> float:
    s = spread.dropna()
    lag, d = s.shift(1).iloc[1:], s.diff().iloc[1:]
    beta = np.polyfit(lag.values, d.values, 1)[0]
    return float(-np.log(2) / beta) if beta < 0 else float("inf")


def _hurst(x: np.ndarray) -> float:
    lags = range(2, 50)
    tau = [np.std(x[l:] - x[:-l]) for l in lags]
    return float(np.polyfit(np.log(list(lags)), np.log(tau), 1)[0])


def pairs(coin_d1: pd.DataFrame, refs: dict[str, pd.DataFrame]) -> dict:
    """Engle-Granger cointegration of log prices vs each reference (BTC, ETH) over 365 and
    180 days: hedge ratio, spread z-score, half-life, Hurst, structural-break flag."""
    from statsmodels.tsa.stattools import coint
    out = {"name": "Cointegrated pairs", "evidence": "A/B", "rows": [], "notes": []}
    for ref, rd in refs.items():
        j = np.log(coin_d1[["close"]].join(rd[["close"]], rsuffix="_r", how="inner")).dropna()
        if len(j) < 200:
            continue
        y365, y180 = j.tail(365), j.tail(180)
        p365 = float(coint(y365["close"], y365["close_r"])[1])
        p180 = float(coint(y180["close"], y180["close_r"])[1])
        beta = float(np.polyfit(y365["close_r"], y365["close"], 1)[0])
        spread = y365["close"] - beta * y365["close_r"]
        z = float((spread.iloc[-1] - spread.mean()) / spread.std())
        hl = _half_life(spread)
        hu = _hurst(spread.values)
        broken = p365 < 0.05 and p180 > 0.20
        ok = p365 < 0.05 and hl < 60 and hu < 0.5 and not broken
        action = ("long coin / short " + ref if z < -2 else "short coin / long " + ref if z > 2 else "no trade (|z| < 2)")
        out["rows"].append({"reference": ref, "coint_p_365d": round(p365, 3), "coint_p_180d": round(p180, 3),
                            "hedge_ratio": round(beta, 3), "z_score": round(z, 2), "half_life_days": round(hl, 1),
                            "hurst": round(hu, 2), "structural_break": broken, "tradable_pair": ok,
                            "signal": action if ok else "none"})
    if not out["rows"]:
        out["status"] = "N/A"; out["notes"].append("Not enough overlapping history."); return out
    good = [r for r in out["rows"] if r["tradable_pair"]]
    out["status"] = "CANDIDATE" if good else "REJECTED"
    for r in out["rows"]:
        out["notes"].append(
            f"vs {r['reference']}: cointegration p={r['coint_p_365d']} (365d) / {r['coint_p_180d']} (180d), "
            f"hedge ratio {r['hedge_ratio']}, z {r['z_score']:+.2f}, half-life {r['half_life_days']}d, "
            f"Hurst {r['hurst']}{' — STRUCTURAL BREAK' if r['structural_break'] else ''} → "
            f"{'tradable, ' + r['signal'] if r['tradable_pair'] else 'not a stable pair'}.")
    out["notes"].append("Correlation is not mean reversion; the short leg needs margin/futures (not enabled). Research only.")
    return out


# ------------------------------------------------------------ funding carry
def funding_carry(fc, symbol: str) -> dict:
    """Delta-neutral carry: long spot + short USD-M perpetual, collecting funding when positive."""
    out = {"name": "Funding / basis carry (delta-neutral)", "evidence": "A/B", "notes": [], "available": False}
    try:
        hist = fc.funding_history(symbol, 90)
        mark = fc.mark_price(symbol)
    except Exception as e:
        out["status"] = "N/A"
        out["notes"].append(f"No USD-M perpetual data for {symbol} ({str(e)[:80]}).")
        return out
    rates = np.array([float(h["fundingRate"]) for h in hist]) if hist else np.array([])
    if len(rates) < 20:
        out["status"] = "N/A"; out["notes"].append("Perpetual listed too recently."); return out
    mean8h = float(rates.mean())
    pos = float((rates > 0).mean())
    basis = float(mark["markPrice"]) / float(mark["indexPrice"]) - 1
    cost = 2 * (FEE + SLIPPAGE) + 2 * (FUT_TAKER + SLIPPAGE)       # open + close both legs
    exp30 = mean8h * 90
    net30 = exp30 - cost
    ok = net30 > cost and pos >= 0.7
    out.update({"available": True, "status": "CANDIDATE" if ok else "REJECTED",
                "mean_rate_8h": mean8h, "annualised_pct": round(mean8h * 3 * 365 * 100, 1),
                "positive_share": round(pos, 2), "basis_pct": round(basis * 100, 3),
                "last_rate": float(mark.get("lastFundingRate", 0)), "round_trip_cost_pct": round(cost * 100, 2),
                "expected_30d_net_pct": round(net30 * 100, 2)})
    out["notes"] += [f"Last 30 days: mean funding {mean8h * 100:.4f}% per 8h (≈{out['annualised_pct']}%/yr), "
                     f"positive {pos:.0%} of the time; basis {basis * 100:+.3f}%.",
                     f"Holding 30 days would collect ≈{exp30 * 100:.2f}% vs {cost * 100:.2f}% round-trip costs → "
                     f"net {net30 * 100:+.2f}%.",
                     "Not free yield: funding can flip, the short leg can be liquidated on a spike, and both venues carry "
                     "counterparty risk. Needs a futures account — research only."]
    return out


# ---------------------------------------------------------- market making
def market_making(liq_metrics: dict, m15: pd.DataFrame) -> dict:
    spread = liq_metrics.get("spread_bps", 99)
    fee_rt_bps = 2 * MAKER_FEE * 1e4
    vol15 = float(m15["close"].pct_change().tail(96 * 7).std() * 1e4)
    need = fee_rt_bps + 0.5 * vol15 * 0.1          # fees + small adverse-selection allowance
    ok = spread > need
    return {"name": "Market making (spread capture)", "evidence": "B (execution-dependent)",
            "status": "CANDIDATE" if ok else "REJECTED",
            "spread_bps": spread, "maker_round_trip_bps": round(fee_rt_bps, 1), "vol_15m_bps": round(vol15, 1),
            "notes": [f"Quoted spread {spread:.1f} bps vs maker fees {fee_rt_bps:.0f} bps per round trip "
                      f"(set MAKER_FEE in analyses.py to your tier); 15m volatility {vol15:.0f} bps.",
                      ("Captured spread could exceed fees — model queues/latency in hftbacktest before any paper run."
                       if ok else "Spread is far below your fees: market making loses money at this fee tier."),
                      "Needs tick/order-book infrastructure (hftbacktest → Hummingbot paper). Research only."]}


# ---------------------------------------------------- short-horizon reversal
def reversal_15m(m15: pd.DataFrame) -> dict:
    r = m15["close"].pct_change().dropna()
    prev, nxt = r.shift(1).iloc[1:], r.iloc[1:]
    long_only = nxt[prev < 0]                                   # spot: buy after a down candle
    both = (-np.sign(prev) * nxt)
    edge_ls, edge_l = float(both.mean() * 1e4), float(long_only.mean() * 1e4)
    t = float(both.mean() / (both.std() / np.sqrt(len(both)))) if both.std() > 0 else 0.0
    cost_taker = 2 * (FEE + SLIPPAGE) * 1e4
    cost_maker = 2 * MAKER_FEE * 1e4
    return {"name": "15m candle reversal", "evidence": "A (statistically real, economically marginal)",
            "status": "EXCLUDED",
            "gross_edge_bps": round(edge_ls, 2), "long_only_edge_bps": round(edge_l, 2), "t_stat": round(t, 2),
            "notes": [f"Gross reversal edge {edge_ls:+.2f} bps/trade (t={t:.1f}); long-only {edge_l:+.2f} bps "
                      f"vs costs {cost_taker:.0f} bps taker / {cost_maker:.0f} bps maker per round trip.",
                      "Kept as a research feature only — not tradable for a retail fee tier."]}


# -------------------------------------------------------- range-gated grid
def grid(df: pd.DataFrame, reg: dict) -> dict:
    last = df.iloc[-1]
    hi, lo = float(df["high"].tail(50).max()), float(df["low"].tail(50).min())
    width = hi / lo - 1
    rt = 2 * (FEE + SLIPPAGE)
    step = max(3 * rt, width / 10)
    levels = int(min(10, width // step))
    enabled = reg["trend_state"] == "range" and reg["vol_state"] != "high" and levels >= 3
    why = []
    if reg["trend_state"] != "range":
        why.append(f"market is in a {reg['trend_state']} state (grids bleed when price trends out of range)")
    if reg["vol_state"] == "high":
        why.append("volatility is in its top 20% for the year")
    if levels < 3:
        why.append(f"range {width:.1%} too narrow for ≥3 levels above {3 * rt:.1%} cost-covering steps")
    return {"name": "Range-gated grid", "evidence": "C/D", "status": "ENABLED" if enabled else "DISABLED",
            "range_low": lo, "range_high": hi, "levels": levels, "step_pct": round(step * 100, 2),
            "range_break_stop": lo - float(last["atr"]),
            "notes": [f"50-bar range {lo:.6g} – {hi:.6g} ({width:.1%}); {levels} levels of {step:.2%}.",
                      ("Grid permitted: max inventory = levels × unit, range-break stop at "
                       f"{lo - float(last['atr']):.6g}, auto-shutdown after 7 days." if enabled
                       else "Grid DISABLED: " + "; ".join(why) + "."),
                      "No uncapped averaging / martingale is ever allowed."]}


# ------------------------------------------------------- narrative phase
def narrative_phase(d1: pd.DataFrame, btc_d1: pd.DataFrame, quote_vol_24h: float) -> dict:
    """Heuristic version of the regime rotation traders discuss on X: blue-chips in dumps,
    strongest names on regime shifts, low-caps only in euphoria. Informational only."""
    b = btc_d1
    long = b["ema200"].fillna(b["ema50"])
    r30 = float(b["close"].iloc[-1] / b["close"].iloc[-31] - 1)
    dist = float(b["close"].iloc[-1] / long.iloc[-1] - 1)
    crossed = bool(((b["close"] > long) & (b["close"].shift() <= long.shift())).tail(30).any())
    if r30 < -0.15 or (dist < 0 and r30 < 0):
        phase, favour = "dump / risk-off", "blue-chip"
    elif dist > 0.30 and r30 > 0.20:
        phase, favour = "euphoria", "low-cap"
    elif crossed:
        phase, favour = "regime shift up", "mid-cap"
    else:
        phase, favour = "normal", "any"
    bucket = "blue-chip" if quote_vol_24h >= 300e6 else "mid-cap" if quote_vol_24h >= 20e6 else "low-cap"
    fits = favour in ("any", bucket)
    ath_dd = float(d1["close"].iloc[-1] / d1["high"].max() - 1)
    return {"name": "Narrative / market-phase rotation", "evidence": "D", "status": "FITS" if fits else "OFF-PHASE",
            "phase": phase, "bucket": bucket, "favoured": favour,
            "notes": [f"BTC 30d {r30:+.1%}, {dist:+.1%} vs its long EMA{' (crossed above in the last 30 days)' if crossed else ''} "
                      f"→ phase **{phase}**, which historically favours {favour} coins.",
                      f"This coin is {bucket} by liquidity (${quote_vol_24h / 1e6:,.0f}M/24h) and "
                      f"{ath_dd:.0%} from its all-time high → {'fits' if fits else 'does not fit'} the phase.",
                      "Social-media heuristic without narrative/community data — context only, never a signal."]}


EXCLUDED = [
    ("Copy-trading / smart-money wallet tracking",
     "Needs on-chain/DEX wallet feeds, not Binance data; arriving even ~60s late usually erases the edge, "
     "and followers can end up providing exit liquidity to the wallets they copy."),
    ("Low-cap / new-token sniping", "DEX launch data, rug and liquidity risks — outside a Binance Spot research tool."),
    ("Martingale / unlimited DCA", "Adds to losers without a cap — excluded from live use by design."),
    ("Cross-exchange arbitrage", "Needs accounts and capital on several venues, low latency and exchange-risk controls; "
     "the spot-perpetual version is covered by the funding/basis carry analysis."),
]


# ------------------------------------------------------ funding / OI squeeze
def squeeze(funding_hist, deriv: dict, df: pd.DataFrame, hi_idx) -> dict:
    """Crowding + positioning: extreme funding with rising open interest, confirmed by a price reclaim
    (short squeeze) or breakdown (long squeeze). OI history is only ~30 days on Binance, so this is a
    current-state read, not a backtested strategy."""
    out = {"name": "Funding / OI squeeze", "evidence": "C", "status": "N/A", "notes": []}
    if funding_hist is None or len(funding_hist) < 30 or "oi_change_24h_pct" not in deriv:
        out["notes"].append("Needs a USD-M perpetual with funding and open-interest history.")
        return out
    r = funding_hist["rate"].tail(90)
    z = float((r.iloc[-1] - r.mean()) / r.std()) if r.std() > 0 else 0.0
    oi24 = deriv.get("oi_change_24h_pct") or 0
    c, H, L = df["close"].values, df["high"].values, df["low"].values
    ph = hi_idx[hi_idx + 3 <= len(df) - 1]
    reclaim = len(ph) and c[-1] > H[ph[-1]]
    breakdown = c[-1] < L[-21:-1].min()
    if (r.iloc[-1] < 0 and z < -1.5) and oi24 > 5 and reclaim:
        out["status"] = "CANDIDATE (short squeeze)"
    elif (r.iloc[-1] > 0.0003 or z > 2) and oi24 > 5 and breakdown:
        out["status"] = "WARNING (long squeeze risk)"
    else:
        out["status"] = "NONE"
    out.update({"funding_now_8h": float(r.iloc[-1]), "funding_z_30d": round(z, 2), "oi_change_24h_pct": oi24,
                "long_short_accounts": deriv.get("long_short_ratio_accounts"),
                "top_trader_long_short": deriv.get("top_trader_long_short_positions")})
    out["notes"] += [f"Funding {r.iloc[-1] * 100:.4f}%/8h (z {z:+.1f} vs 30 days), open interest {oi24:+.1f}% in 24h, "
                     f"global long/short {deriv.get('long_short_ratio_accounts')}, top traders {deriv.get('top_trader_long_short_positions')}.",
                     {"CANDIDATE (short squeeze)": "Shorts are crowded, OI is building and price reclaimed the last swing high.",
                      "WARNING (long squeeze risk)": "Longs are crowded, OI is building and price broke its 20-candle low — "
                                                     "forced selling risk; avoid new longs.",
                      "NONE": "No squeeze setup: crowding, OI build-up and a price trigger are not all present."}[out["status"]],
                     "Liquidation-level maps are not available from free sources."]
    return out


# ------------------------------------------------------------ DCA simulator
def dca(d1: pd.DataFrame, weekly_usd: float = 100.0, days: int = 365) -> dict:
    """Scheduled accumulation (portfolio method, not a trading signal): buy every 7 days."""
    d = d1.tail(days)
    if len(d) < 60:
        return {"name": "DCA accumulation (weekly)", "evidence": "n/a", "status": "N/A", "notes": ["Not enough history."]}
    buys = d.iloc[::7]
    cost = FEE + SLIPPAGE
    units = (weekly_usd * (1 - cost) / buys["close"]).cumsum()
    invested = pd.Series(weekly_usd, index=buys.index).cumsum()
    val_ = units.reindex(d.index, method="ffill") * d["close"]
    inv = invested.reindex(d.index, method="ffill")
    ratio = val_ / inv
    lump = (d["close"].iloc[-1] / d["close"].iloc[0] - 1) * 100
    avg_cost = float(invested.iloc[-1] / units.iloc[-1])
    ret = float(ratio.iloc[-1] - 1) * 100
    return {"name": "DCA accumulation (weekly)", "evidence": "n/a", "status": "INFO",
            "invested": float(invested.iloc[-1]), "value": float(val_.iloc[-1]), "return_pct": round(ret, 1),
            "lump_sum_return_pct": round(lump, 1), "avg_cost": avg_cost,
            "worst_drawdown_vs_invested_pct": round(float((ratio.min() - 1) * 100), 1),
            "notes": [f"${weekly_usd:.0f} every 7 days for {len(d)} days: invested ${invested.iloc[-1]:,.0f}, now "
                      f"${val_.iloc[-1]:,.0f} ({ret:+.1f}%); average cost {avg_cost:.6g} vs price {d['close'].iloc[-1]:.6g}. "
                      f"Lump sum at the start: {lump:+.1f}%. Worst point: {(ratio.min() - 1) * 100:+.1f}% vs money in.",
                      "Accumulation method, not a directional signal — shown for context only."]}
