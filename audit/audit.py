"""Binance Spot Coin Audit Bot — strategy research library edition.

Usage:
    python audit.py SOL                  # asks which timeframe to test
    python audit.py SOL --tf 1h          # 15m | 1h | 4h | 1d
    python audit.py SOL --tf all         # test every timeframe and compare
    python audit.py SOL ARB --tf 4h      # several coins + ranking
    python audit.py SOL --watch          # also add SOL/USDT to the Freqtrade dry-run whitelist
    python audit.py SOL --no-ml          # skip the meta-labeler (faster)
    python audit.py --review             # grade past audits against what happened

    python audit.py SOL --as-of 2026-06-01   # point-in-time replay (what would the audit have said?)
    python audit.py --demo               # full offline run on synthetic data (proves the install works)
    python audit.py SOL --offline research/data/clean   # run from local parquet files (no network)

Pipeline per coin + timeframe:
  market checks -> regime classifier -> strategy library (27 modules, each through
  12 validation gates) -> chart patterns -> crypto-wide + macro regime -> research analyses ->
  ML meta-labeler on the strategies' own signals -> standard signal records -> verdict + trade plan.
Read-only: public market data, no API key, no orders. Research tool, not financial advice.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import analyses
import analytics
import dashboard
import market as mk
import notify
import overfit
import patterns as pt
import portfolio
import checks
import indicators as ind
import meta_model
import regime as rg
import sizing
import strategies as st
import validation as val
from binance_client import (BinanceClient, BinanceError, FuturesClient, OfflineClient, OfflineFuturesClient,
                            to_ms)
from settings import CFG, DATA_DIR

ROOT = Path(__file__).resolve().parent
REPORTS = DATA_DIR / "reports"
LOG = DATA_DIR / "logs" / "audits.csv"
SIGNAL_LOG = DATA_DIR / "logs" / "signals.csv"
TRACK_RECORD = DATA_DIR / "logs" / "track_record.json"
KLINE_CACHE = DATA_DIR / "cache" / "klines"
DEMO_DIR = DATA_DIR / "cache" / "demo"
FT_CONFIG = ROOT.parent / "freqtrade" / "user_data" / "config.json"

TIMEFRAMES = ["15m", "1h", "4h", "1d"]
BARS = {"15m": 5000, "1h": 5000, "4h": 3000, "1d": 1000, "1w": 400}
TF_NOTE = {"15m": "scalp/intraday — ~52 days of history, costs weigh heavily",
           "1h": "intraday swing — ~7 months of history",
           "4h": "swing (default) — ~16 months of history",
           "1d": "position — full listing history"}
WEIGHTS = dict(CFG["weights"])
RISK_BY_STATUS = {"ACCEPTED": CFG["risk"]["accepted"], "CANDIDATE": CFG["risk"]["candidate"], "REJECTED": 0.0}
RISK_PER_TRADE = CFG["risk"]["plan_risk_per_trade"]
VERDICT_T = CFG["verdict"]
LOG_FIELDS = ["audit_time_utc", "symbol", "timeframe", "price", "score", "verdict", "entry_low", "entry_high",
              "stop", "target1", "target2", "horizon_days", "accepted", "candidates", "approved_signals",
              "outcome", "outcome_return_pct", "outcome_checked_utc"]
SIGNAL_FIELDS = ["strategy_id", "symbol", "timeframe", "timestamp", "direction", "confidence",
                 "expected_holding_bars", "entry_type", "stop_distance_atr", "regime_required", "regime_ok",
                 "status", "max_risk_fraction", "evidence_version", "decision",
                 # v6 extras: sizing inputs, meta-labeler decision and the fields --review grades
                 "price", "atr", "stop_px", "target_px", "wf_win_rate", "meta_decision", "meta_p",
                 "size_multiplier", "kelly_risk_fraction", "outcome", "outcome_return_pct", "outcome_checked_utc"]


def fmt_px(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.8f}".rstrip("0")


def bars_to_days(bars: int, tf: str) -> float:
    return round(bars * rg.TF_MINUTES[tf] / 1440, 1)


# --------------------------------------------------------------------- data
class Fetcher:
    """Caches klines per (symbol, interval) so overlapping requests are fetched once."""

    def __init__(self, client: BinanceClient):
        self.c, self.cache = client, {}

    def get(self, symbol: str, interval: str, bars: int) -> pd.DataFrame:
        key = (symbol, interval)
        if key not in self.cache or len(self.cache[key]) < bars:
            self.cache[key] = ind.add_all(self.c.klines(symbol, interval, bars))
        return self.cache[key].tail(bars)


def ask_timeframe() -> str:
    print("\nWhich timeframe should the strategies be tested on?")
    for t in TIMEFRAMES:
        print(f"  {t:<4} {TF_NOTE[t]}")
    print("  all  test every timeframe and compare")
    while True:
        a = input("Timeframe [15m/1h/4h/1d/all] (Enter = 4h): ").strip().lower() or "4h"
        if a in TIMEFRAMES + ["all"]:
            return a
        print("  please type one of: 15m 1h 4h 1d all")


# -------------------------------------------------------------------- plan
def trade_plan(df: pd.DataFrame, d1: pd.DataFrame, tick: float, stop_atr: float, horizon: str) -> dict:
    last = df.iloc[-1]
    price, a = float(last["close"]), float(last["atr"])
    entry_low = max(min(float(last["ema20"]), price), price - 0.75 * a)
    entry_low = min(entry_low, price - 0.25 * a)
    swing_low = float(df["low"].tail(20).min())
    stop = min(swing_low - 0.25 * a, price - stop_atr * a)
    stop = max(stop, entry_low - 3.0 * a)
    ref = (entry_low + price) / 2
    risk = ref - stop
    rnd = lambda x: round(round(x / tick) * tick, 12) if tick else x
    stop_pct = risk / ref
    return {"price": price, "entry_low": rnd(entry_low), "entry_high": rnd(price), "stop": rnd(stop),
            "target1": rnd(ref + 1.5 * risk), "target2": rnd(ref + 3.0 * risk),
            "stop_distance_pct": round(stop_pct * 100, 2),
            "resistance_20_bars": float(df["high"].tail(20).max()), "resistance_90d": float(d1["high"].tail(90).max()),
            "position_size_pct_of_account": round(min(RISK_PER_TRADE / stop_pct, 1.0) * 100, 1),
            "horizon": horizon}


# ------------------------------------------------------------ sizing (v6)
def execution_plan(plan: dict, liq: dict, lead: dict | None, res, df: pd.DataFrame, tf: str,
                   signal: dict | None) -> dict:
    """Kelly stake from the lead strategy's own walk-forward trades, volatility-target scaling and the
    slippage the plan's position would actually pay against today's order book."""
    R = CFG["risk"]
    stop_pct = plan["stop_distance_pct"] / 100
    ask_depth = liq.get("depth_1pct_usd", 0) * (1 - liq.get("book_bid_share", 0.5))
    risk = signal["max_risk_fraction"] if signal else RISK_PER_TRADE
    out = {"account_usd": R["account_usd"], "risk_fraction_used": risk,
           "vol_target_scale": sizing.vol_target(float(df["atr_pct"].iloc[-1]), 0.02, 1440 / rg.TF_MINUTES[tf])}
    out["execution"] = sizing.size_plan(R["account_usd"], risk, stop_pct, plan["price"], ask_depth,
                                        liq.get("spread_bps", 10.0), val.SLIPPAGE, val.FEE)
    if res is not None and len(res.wf_trade_returns) >= 10:
        k = sizing.kelly(res.wf_trade_returns)
        out["kelly"] = {**k, "strategy": lead["id"],
                        "risk_fraction": sizing.risk_fraction(k["suggested_capital_fraction"], stop_pct, RISK_BY_STATUS[lead["status"]])}
    return out


def load_track_record() -> dict:
    try:
        return json.loads(TRACK_RECORD.read_text(encoding="utf-8")) if TRACK_RECORD.exists() else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------- library check
def library_check(rows: list[dict]) -> dict:
    acc_on = [r for r in rows if r["status"] == "ACCEPTED" and r["signal_now"] and r["regime_ok"]]
    cand_on = [r for r in rows if r["status"] == "CANDIDATE" and r["signal_now"] and r["regime_ok"]]
    acc = [r for r in rows if r["status"] == "ACCEPTED"]
    cand = [r for r in rows if r["status"] == "CANDIDATE"]
    s = 35 + 22 * min(len(acc_on), 2) + 10 * min(len(cand_on), 2) + 6 * min(len(acc), 3) + 2 * min(len(cand), 3)
    if not acc and not cand:
        s = 25
    notes = [f"{len(acc)} ACCEPTED, {len(cand)} CANDIDATE, {len(rows) - len(acc) - len(cand)} REJECTED "
             f"of {len(rows)} strategies after {val.N_GATES} validation gates."]
    alpha = CFG["gates"]["fdr_alpha"]
    survivors = [r["id"] for r in acc + cand if r.get("fdr_q") is not None and r["fdr_q"] < alpha]
    if acc or cand:
        notes.append(f"Library-wide false-discovery control (Benjamini-Hochberg, q<{alpha}): "
                     f"{len(survivors)} of {len(acc) + len(cand)} non-rejected strategies keep significance after "
                     f"testing {len(rows)} strategies on the same data" + (": " + ", ".join(survivors) if survivors else "") + ".")
    if acc_on or cand_on:
        notes.append("Active approved signal(s) now: " + ", ".join(r["id"] for r in acc_on + cand_on) + ".")
    else:
        notes.append("No validated strategy has an active signal in a matching regime on the last closed candle.")
    return {"name": "Strategy library (validated)", "score": float(np.clip(s, 0, 100)),
            "status": "pass" if s >= 65 else "warn" if s >= 40 else "fail", "notes": notes,
            "metrics": {"accepted": len(acc), "candidates": len(cand), "active_approved": len(acc_on) + len(cand_on)}}


def meta_check(m: dict) -> dict:
    name = "ML meta-labeler"
    if not m.get("available"):
        return {"name": name, "score": 50.0, "status": "warn", "weight_mult": 0.0, "metrics": m,
                "notes": [f"Meta-labeler skipped: {m.get('reason')}."]}
    notes = [f"Scored {m['events']} past strategy signals (uniqueness-weighted, mean weight {m.get('uniqueness_mean', 1.0)}, "
             f"min {m.get('uniqueness_min', 1.0)}). Walk-forward AUC {m['wf_auc']}, "
             f"filtering lifted average trade return by {m['wf_filter_lift_pct']:+.3f} pts; "
             f"holdout AUC {m['holdout_auc']}, lift {m['holdout_filter_lift_pct']:+.3f} pts.",
             f"Top drivers: {', '.join(m['top_features'])}.",
             f"Calibration: {'isotonic on out-of-fold predictions' if m.get('calibrated') else 'not enough out-of-fold data'} "
             f"(Brier {m.get('calibration_brier', 'n/a')}); conformal coverage {1 - m.get('conformal_alpha', 0.2):.0%} "
             f"(q̂ {m.get('conformal_qhat', 'n/a')}); feature drift PSI {m.get('drift', {}).get('max_psi', 'n/a')} "
             f"on {m.get('drift', {}).get('feature', 'n/a')}"
             + (" — **drift warning: model switched off**." if m.get("drift", {}).get("warning") else ".")]
    if m["has_edge"] and m["active_probs"]:
        best = max(m["active_probs"].values())
        s = 50 + (best - m["accept_threshold"]) * 200
        notes.append("Active signals (calibrated P(win) → conformal decision, size multiplier): " + ", ".join(
            f"{k} {v['p']:.0%} → {v['decision']}, ×{v['size_multiplier']:.2f}" for k, v in (m.get("active") or {}).items()) + ".")
        mult = 1.0
    else:
        s, mult = 50, 0.0
        notes.append("No proven filtering edge (or no active signals) — excluded from the verdict; "
                     "signals fall back to their walk-forward win rates."
                     + (f" Reason: {m['edge_reason']}." if m.get("edge_reason") else ""))
    s = float(np.clip(s, 0, 100))
    return {"name": name, "score": round(s, 1), "status": "pass" if s >= 65 else "warn" if s >= 40 else "fail",
            "notes": notes, "metrics": m, "weight_mult": mult}


def verdict(results, liq, rs, any_approved: bool):
    tot = wsum = 0.0
    for r in results:
        w = WEIGHTS[r["name"]] * r.get("weight_mult", 1.0)
        tot += w * r["score"]; wsum += w
    score, flags = tot / wsum, []
    if liq["metrics"].get("quote_volume_24h", 0) < VERDICT_T["min_volume_usd"]:
        flags.append(f"Illiquid (<${VERDICT_T['min_volume_usd'] / 1e6:g}M 24h volume) — verdict forced to AVOID.")
        score = min(score, VERDICT_T["neutral"] - 5)
    if not rs["metrics"].get("btc_risk_on", True):
        flags.append("BTC is risk-off — verdict capped at WATCHLIST."); score = min(score, 64.9)
    if not any_approved:
        flags.append("No validated strategy signal is active — verdict capped at WATCHLIST."); score = min(score, 64.9)
    v = ("FAVORABLE" if score >= VERDICT_T["favorable"] else "WATCHLIST" if score >= VERDICT_T["watchlist"]
         else "NEUTRAL" if score >= VERDICT_T["neutral"] else "AVOID")
    return round(score, 1), v, flags


VERDICT_TEXT = {
    "FAVORABLE": "A validated strategy has an approved signal and market conditions agree. Use the plan below; size by risk.",
    "WATCHLIST": "Some conditions line up, but no validated signal is confirmed. Wait.",
    "NEUTRAL": "No clear edge right now. Better opportunities likely exist elsewhere.",
    "AVOID": "Conditions are unfavourable for a spot long. Stay out.",
}


# -------------------------------------------------------------------- audit
def macro_check(mreg: dict | None, m: dict, is_btc: bool) -> dict:
    name = "Crypto-wide & macro regime"
    if not mreg:
        return {"name": name, "score": 50.0, "status": "warn", "weight_mult": 0.0, "metrics": {},
                "notes": [f"Market layer unavailable: {m.get('reason', 'skipped')} — excluded from the verdict."]}
    sc = mreg["btc_score"] if is_btc else mreg["alt_score"]
    items = mreg["btc_items"] if is_btc else mreg["alt_items"]
    met = [f"{i['condition']} ({i['points']:+d})" for i in items if i["met"]]
    notes = [f"Regime: **{mreg['regime']}** — {mreg['behaviour']}",
             f"{'BTC' if is_btc else 'Altcoin-long eligibility'} score {sc:.0f}/100 "
             f"({'favourable' if sc >= 70 else 'selective setups only' if sc >= 50 else 'avoid new longs'}).",
             "Scoring conditions met: " + ("; ".join(met) if met else "none") + "."]
    return {"name": name, "score": sc, "status": "pass" if sc >= 70 else "warn" if sc >= 50 else "fail",
            "notes": notes, "metrics": {"regime": mreg["regime"], "score": sc}}


def audit(client, fut, coin: str, quote: str, tf: str, use_ml: bool, use_market: bool = True,
          now: datetime | str | None = None, strategies: list | None = None) -> dict:
    """One audit. `client` is a BinanceClient or OfflineClient; `now` is the wall clock for the stale-data
    check (None = real time, "data" = the last candle's close, or a datetime for --as-of replays);
    `strategies` optionally restricts the library (ids, families or name fragments)."""
    coin = coin.upper().strip()
    symbol = coin if coin.endswith(quote) and len(coin) > len(quote) else coin + quote
    info = client.symbol_info(symbol)
    if info is None:
        raise BinanceError(f"{symbol} is not listed on Binance Spot.")
    if info.get("status") != "TRADING":
        raise BinanceError(f"{symbol} status is {info.get('status')} — not tradable.")
    tick = next((float(f["tickSize"]) for f in info.get("filters", []) if f.get("filterType") == "PRICE_FILTER"), 0.0)
    base = info.get("baseAsset")
    is_btc = base == "BTC"
    btc_sym = "BTC" + quote if client.symbol_info("BTC" + quote) else "BTCUSDT"
    eth_sym = "ETH" + quote if client.symbol_info("ETH" + quote) else "ETHUSDT"

    print(f"  fetching {symbol} data ({tf} + context timeframes) ...", flush=True)
    F = Fetcher(client)
    d1 = F.get(symbol, "1d", 1000)
    if len(d1) < 60:
        raise BinanceError(f"{symbol} has only {len(d1)} days of history — too new to audit.")
    df = F.get(symbol, tf, BARS[tf])
    htf_name = rg.HIGHER_TF[tf]
    htf = F.get(symbol, htf_name, BARS[htf_name] if htf_name != "1d" else 1000)
    h4, h1, m15 = F.get(symbol, "4h", 1000), F.get(symbol, "1h", 1000), F.get(symbol, "15m", 3000)
    btc_d1 = d1 if is_btc else F.get(btc_sym, "1d", 1000)
    btc_tf = df if is_btc else F.get(btc_sym, tf, BARS[tf])
    eth_d1 = None if base == "ETH" else F.get(eth_sym, "1d", 1000)
    if len(df) < 600:
        raise BinanceError(f"Only {len(df)} {tf} candles — not enough history on {tf}"
                           + (" (pick a lower timeframe or wait for more history)." if tf == "1d"
                              else " (pick a higher timeframe for this coin)."))
    if now is None:
        now = datetime.now(timezone.utc)
    elif now == "data":
        now = (df.index[-1] + pd.Timedelta(minutes=rg.TF_MINUTES[tf])).to_pydatetime()
    ticker, book = client.ticker_24h(symbol), client.order_book(symbol)

    print("  market checks + regime ...", flush=True)
    liq = checks.liquidity(ticker, book)
    rs = checks.relative_strength(d1, btc_d1, is_btc)
    results = [liq, checks.trend(d1, h4, h1), checks.momentum(df, d1, tf), checks.volatility(d1, h1), rs,
               checks.flow(m15, h1, d1)]
    btc_d1x = btc_d1.assign(risk_on=(btc_d1["close"] > btc_d1["ema200"].fillna(btc_d1["ema50"])).astype(float))
    ctx = {"tf": tf, "is_btc": is_btc,
           "btc_close": btc_tf["close"].reindex(df.index).ffill(),
           "btc_risk_on": rg.align(df.index, tf, btc_d1x, "1d", ["risk_on"])["risk_on"]}
    ha = rg.align(df.index, tf, htf, htf_name, ["close", "ema200"])
    ctx["htf_close"], ctx["htf_ema200"] = ha["close"], ha["ema200"]
    fs = None
    if fut:
        try:
            fs = fut.funding_series(symbol, int(df.index[0].timestamp() * 1000))
            if len(fs):
                f3 = fs["rate"].rolling(9, min_periods=3).mean()          # trailing 3 days of settlements
                ctx["funding_3d"] = rg.align_events(df.index, tf, f3.index, f3.values)
        except Exception:
            pass
    funding = analyses.funding_carry(fut, symbol) if fut else {"status": "N/A", "notes": ["skipped"], "name": "Funding / basis carry (delta-neutral)"}
    reg = rg.classify(df, tf, rs["metrics"]["btc_risk_on"], liq["metrics"].get("quote_volume_24h", 0) >= 1e6, funding)

    print("  scanning chart patterns + candlesticks ...", flush=True)
    scan = pt.scan(df, val.COST)
    m, mreg = {"available": False, "reason": "disabled with --no-market"}, None
    if use_market:
        print("  building crypto-wide + macro regime (cached after the first run) ...", flush=True)
        try:
            cf = {"15m": m15, "1h": h1, "4h": h4, "1d": d1}
            bf = cf if is_btc else {"15m": F.get(btc_sym, "15m", 3000), "1h": F.get(btc_sym, "1h", 1000),
                                    "4h": F.get(btc_sym, "4h", 1000), "1d": btc_d1}
            m = mk.snapshot(client, fut, symbol, base, cf, bf, liq["metrics"], client.all_symbols())
            if m.get("available"):
                bh4 = bf["4h"]
                bhi, blo = pt.pivots(bh4)
                mreg = mk.classify(m, bh4, btc_d1, is_btc, liq["metrics"].get("spread_bps", 99) <= 10
                                   and liq["metrics"].get("quote_volume_24h", 0) >= 20e6,
                                   pt.structure(bh4, bhi, blo, len(bh4) - 1))
        except Exception as e:                                   # market layer must never break an audit
            m = {"available": False, "reason": f"error: {str(e)[:120]}"}
    results.append(macro_check(mreg, m, is_btc))

    library = st.select(strategies)
    print(f"  validating {len(library)} strategies on {len(df)} {tf} candles ...", flush=True)
    rows, validated = [], []
    for s in library:
        res = val.validate(s, df, ctx)
        ok = rg.matches(s.regime_required, reg)
        row = {"id": s.id, "name": s.name, "family": s.family, "evidence": s.evidence,
               "evidence_note": s.evidence_note, "regime_required": s.regime_required, "regime_ok": ok,
               "status": res.status, "params": res.params, "gates": res.gates,
               "gates_passed": sum(res.gates.values()), "wf": res.wf, "wf_cost2x": res.wf_cost2x,
               "holdout": res.holdout, "fold_returns_pct": [None if x is None else round(x * 100, 1) for x in res.fold_returns],
               "neighbour_share": res.neighbour_share, "concentration": res.concentration,
               "monte_carlo": res.monte_carlo, "signal_now": res.signal_now, "in_position_now": res.in_position_now,
               "stop_atr": res.params.get("stop_atr", s.stop_atr), "horizon_bars": s.horizon_bars,
               "benchmark": res.benchmark, "overfit": res.overfit, "levels_now": res.levels_now}
        if isinstance(s, st.Pullback):
            row["ablation"] = st.ablation(s, df, ctx, res.params, int(len(df) * (1 - val.HOLDOUT)))
        rows.append(row)
        validated.append((s, res))
    for r, q in zip(rows, overfit.benjamini_hochberg([r["monte_carlo"]["p_value"] for r in rows])):
        r["fdr_q"] = round(float(q), 3)

    meta = {"available": False, "reason": "disabled with --no-ml"}
    vm = [v for v in validated if v[1].lookahead_ok]          # never learn from a leaking strategy
    active = [j for j, (_, res) in enumerate(vm) if res.signal_now and res.status != "REJECTED"]
    if use_ml:
        print("  training meta-labeler on the strategies' own signals ...", flush=True)
        meta = meta_model.run(df, ctx, vm, active)
    lib = library_check(rows)
    results += [lib, meta_check(meta)]

    # standard signal records (never orders)
    stamp_close = (df.index[-1] + pd.Timedelta(minutes=rg.TF_MINUTES[tf])).isoformat()
    ev_ver = "walkforward_" + now.strftime("%Y_%m")
    by_id = {s_.id: res for s_, res in validated}
    track = load_track_record()
    price_now, atr_now = float(df["close"].iloc[-1]), float(df["atr"].iloc[-1])
    signals = []
    for r in rows:
        if not r["signal_now"]:
            continue
        act = (meta.get("active") or {}).get(r["id"]) if meta.get("has_edge") else None
        prob = act["p"] if act else None
        conf = prob if prob is not None else r["wf"]["win_rate"]
        reasons, size_mult = [], 1.0
        if r["status"] == "REJECTED":
            reasons.append("strategy failed validation")
        if not r["regime_ok"]:
            reasons.append(f"regime is not '{r['regime_required']}'")
        if act:
            if act["decision"] == "reject":
                reasons.append(f"meta-labeler rejects (calibrated P(win) {prob:.0%})")
            elif act["decision"] == "abstain":
                size_mult = 0.5                                   # conformal set holds both outcomes
            else:
                size_mult = act["size_multiplier"]
        tr = track.get(r["id"])
        if tr and tr.get("drift"):
            reasons.append(f"live track record {tr['live_win_rate']:.0%} over {tr['n']} graded signals is below the "
                           f"backtest {tr['backtest_win_rate']:.0%} (p={tr['p_value']})")
        reasons += mk.gate(r, mreg, is_btc, scan["fresh_bearish"])
        lv = r.get("levels_now") or {}
        stop_px = lv.get("stop_px") if lv.get("stop_px") is not None else price_now - r["stop_atr"] * atr_now
        stop_pct = max((price_now - stop_px) / price_now, 1e-4)
        target_px = lv.get("tp_px") if lv.get("tp_px") is not None else price_now + 2.0 * (price_now - stop_px)
        cap = RISK_BY_STATUS[r["status"]]
        kel = sizing.kelly(by_id[r["id"]].wf_trade_returns) if r["id"] in by_id else {"suggested_capital_fraction": 0.0}
        kelly_risk = sizing.risk_fraction(kel["suggested_capital_fraction"], stop_pct, cap) if cap else 0.0
        risk = min(cap * size_mult, kelly_risk if kelly_risk > 0 else cap * size_mult) if not reasons else 0.0
        signals.append({"strategy_id": r["id"], "symbol": f"{base}/{quote}", "timeframe": tf,
                        "timestamp": stamp_close, "direction": 1, "confidence": round(float(conf), 3),
                        "expected_holding_bars": r["horizon_bars"],
                        "entry_type": "limit" if (lv.get("limit_px") is not None
                                                  or r["family"] in ("pullback", "smart money concepts")) else "market",
                        "stop_distance_atr": round((price_now - stop_px) / atr_now, 2) if atr_now else r["stop_atr"],
                        "regime_required": r["regime_required"],
                        "regime_ok": r["regime_ok"], "status": r["status"],
                        "max_risk_fraction": round(risk, 5),
                        "evidence_version": ev_ver,
                        "decision": "APPROVED" if not reasons else "BLOCKED: " + "; ".join(reasons),
                        "price": price_now, "atr": atr_now, "stop_px": stop_px, "target_px": target_px,
                        "wf_win_rate": r["wf"]["win_rate"], "meta_decision": act["decision"] if act else "n/a",
                        "meta_p": prob, "size_multiplier": size_mult, "kelly_risk_fraction": round(kelly_risk, 5),
                        "outcome": "", "outcome_return_pct": "", "outcome_checked_utc": ""})
    approved = [s for s in signals if s["decision"] == "APPROVED"]

    refs = {}
    if not is_btc:
        refs["BTC"] = btc_d1
    if eth_d1 is not None:
        refs["ETH"] = eth_d1
    research = [analyses.pairs(d1, refs), funding, analyses.market_making(liq["metrics"], m15), analyses.reversal_15m(m15),
                analyses.grid(df, reg),
                analyses.narrative_phase(d1, btc_d1, liq["metrics"].get("quote_volume_24h", 0)),
                analyses.squeeze(fs, (m.get("coin") or {}) if m.get("available") else
                                 (mk.derivatives(fut, symbol) if fut else {}), h4, pt.pivots(h4)[0]),
                analyses.dca(d1)]

    score, v, flags = verdict(results, liq, rs, bool(approved))
    age_min = (now - df.index[-1].to_pydatetime()).total_seconds() / 60
    if age_min > 3 * rg.TF_MINUTES[tf]:
        flags.append(f"STALE DATA: last {tf} candle is {age_min / 60:.1f}h old — kill switch: no signal is valid.")
        for s_ in signals:
            s_["decision"], s_["max_risk_fraction"] = "BLOCKED: stale data", 0.0
        approved = []
        v = "AVOID" if v == "FAVORABLE" else v
    lead = next((r for r in rows if approved and r["id"] == approved[0]["strategy_id"]), None)
    stop_atr = lead["stop_atr"] if lead else 2.0
    hz_bars = lead["horizon_bars"] if lead else 30
    plan = trade_plan(df, d1, tick, stop_atr, f"~{bars_to_days(hz_bars, tf)} days ({hz_bars} x {tf})")
    plan.update(execution_plan(plan, liq["metrics"], lead, by_id.get(lead["id"]) if lead else None, df, tf,
                               approved[0] if approved else None))

    best = max(validated, key=lambda sr: (sr[1].status != "REJECTED", sr[1].wf["expectancy_pct"]))
    bench = df["close"].pct_change().fillna(0.0)
    qret = pd.Series(best[1].trades_dev.bar_ret, index=df.index)
    qm = analytics.metrics(qret, bench)

    return {"symbol": symbol, "base": base, "quote": quote, "timeframe": tf,
            "audit_time_utc": now.strftime("%Y-%m-%d %H:%M"),
            "point_in_time": bool(getattr(client, "as_of_ms", None)), "offline": isinstance(client, OfflineClient),
            "data_until_utc": df.index[-1].strftime("%Y-%m-%d %H:%M"), "candles": len(df),
            "history_days": int((d1.index[-1] - d1.index[0]).days),
            "score": score, "verdict": v, "verdict_text": VERDICT_TEXT[v], "flags": flags, "regime": reg,
            "plan": plan, "checks": results, "strategies": rows, "signals": signals, "research": research,
            "meta": meta, "best_strategy": best[0].id, "best_quantstats": qm, "patterns": scan,
            "market": {k: v for k, v in m.items() if k not in ("series_1d", "series_4h", "pairs")},
            "market_regime": mreg, "track_record": {k: v for k, v in track.items() if k in {r["id"] for r in rows}},
            "_qs": (qret, bench, best[0].name)}


# ------------------------------------------------------------------- output
def _f(x, unit=""):
    if x is None:
        return "—"
    if isinstance(x, (int, float)) and not isinstance(x, bool) and abs(x) >= 1e6:
        return f"{x / 1e9:,.2f}B" if abs(x) >= 1e9 else f"{x / 1e6:,.1f}M"
    return f"{x:+.2f}{unit}" if unit else f"{x:,.4g}"


def market_md(a: dict) -> list:
    m, r = a.get("market") or {}, a.get("market_regime")
    L = ["## Market regime (crypto-wide + macro)", ""]
    if not m.get("available") or not r:
        return L + [f"_Unavailable: {m.get('reason', 'skipped')}. Signals are gated by per-coin checks only._", ""]
    is_btc = a["base"] == "BTC"
    sc = r["btc_score"] if is_btc else r["alt_score"]
    L += [f"**{r['regime']}** — {r['behaviour']}", "",
          f"**{'BTC regime' if is_btc else 'Altcoin-long eligibility'} score: {sc:.0f}/100** "
          f"(70+ favourable · 50–69 selective, ACCEPTED strategies only · <50 no new longs)", "",
          "| Condition | Points | Met |", "|---|---:|---|"]
    L += [f"| {i['condition']} | {i['points']:+d} | {'✅' if i['met'] else '—'} |"
          for i in (r["btc_items"] if is_btc else r["alt_items"])]
    cov = m.get("coverage_vs_coingecko")
    L += ["", "### Dashboard (dominance in percentage points, everything else in %)", "",
          "| Symbol | Value | 4h | 24h | 7d | 30d | 90d | 4H vs EMA200 | 1D vs EMA50 |",
          "|---|---:|---:|---:|---:|---:|---:|---|---|"]
    for d in m["dashboard"]:
        u = " pp" if d["unit"] == "pp" else "%"
        val_ = f"{d['value']:.2f}%" if d["unit"] == "pp" else _f(float(d["value"]))
        t4, t1 = d["trend_4h"], d["trend_1d"]
        L.append(f"| {d['symbol']} | {val_} | {_f(d['4h'], u)} | {_f(d['24h'], u)} | {_f(d['7d'], u)} | "
                 f"{_f(d['30d'], u)} | {_f(d['90d'], u)} | {'above' if t4['above_long_ema'] else 'below'} ({t4['ema20_slope']}) | "
                 f"{'above' if t1['above_long_ema'] else 'below'} ({t1['ema20_slope']}) |")
    if m.get("macro"):
        L += ["", "### Macro (FRED, daily; yields in percentage points)", "",
              "| Series | Value | as of | 5 obs | 20 obs | 60 obs | vs trend | 60-obs breakout |", "|---|---:|---|---:|---:|---:|---|---|"]
        for k, v in m["macro"].items():
            u = " pp" if v["unit"] == "pp" else "%"
            L.append(f"| {k} ({v['series']}) | {v['value']:,.4g} | {v['as_of']} | {_f(v['5obs'], u)} | {_f(v['20obs'], u)} | "
                     f"{_f(v['60obs'], u)} | {'above' if v['trend']['above_long_ema'] else 'below'} | {'yes' if v['broke_higher_60'] else '—'} |")
    c = m.get("coin") or {}
    if c:
        L += ["", f"### {a['base']} vs BTC vs TOTAL3", "", "| Period | Coin | BTC | TOTAL3 | Coin − BTC | Coin − TOTAL3 |",
              "|---|---:|---:|---:|---:|---:|"]
        for per, v in c["performance"].items():
            L.append(f"| {per} | {_f(v['coin'], '%')} | {_f(v['btc'], '%')} | {_f(v['total3'], '%')} | "
                     f"{_f(v['vs_btc'], '%')} | {_f(v['vs_total3'], '%')} |")
        keys = [("market_cap", "Market cap"), ("market_cap_rank", "Rank"), ("fdv", "FDV"), ("volume_24h", "24h volume"),
                ("volume_to_mcap", "Volume / market cap"), ("relative_volume", "Relative volume (1d vs 30d)"),
                ("spread_bps", "Spread (bps)"), ("depth_1pct_usd", "±1% depth"), ("corr_btc_90d", "Correlation BTC 90d"),
                ("corr_eth_90d", "Correlation ETH 90d"), ("open_interest_usd", "Open interest"),
                ("oi_change_24h_pct", "OI change 24h %"), ("oi_change_7d_pct", "OI change 7d %"),
                ("long_short_ratio_accounts", "Long/short (accounts)"), ("top_trader_long_short_positions", "Top traders L/S"),
                ("taker_buy_sell_ratio_24h", "Taker buy/sell 24h")]
        L += ["", "| Field | Value |", "|---|---:|"] + [f"| {lab} | {_f(c[k]) if isinstance(c.get(k), (int, float)) else c.get(k)} |"
                                                    for k, lab in keys if c.get(k) is not None]
        L.append(f"| Not available | {'; '.join(c.get('not_available', []))} |")
    if m.get("events_48h") or m.get("coin_events_7d"):
        L += ["", "Upcoming events: " + "; ".join(f"{e['date_utc']} {e['event']}" for e in m.get("events_48h", []) + m.get("coin_events_7d", []))]
    L += ["", f"_Method: own index from {m.get('coins_in_index')} Binance-listed coins (price × current supply) + "
          f"stablecoins from {m.get('stables_source')}; covers "
          f"{'%.0f%%' % (cov * 100) if cov else 'n/a'} of CoinGecko's total market cap. Not identical to TradingView's "
          f"CRYPTOCAP series — use it for direction and regime, not exact levels._", ""]
    return L


def patterns_md(a: dict) -> list:
    p = a.get("patterns") or {}
    L = ["## Chart patterns & candlesticks (last closed candle)", "",
         f"Market structure ({a['timeframe']} pivots): **{p.get('structure', '?')}**", ""]
    if p.get("pending_bullish"):
        L += ["Forming, **not yet confirmed** (needs a candle close above the level with volume):", "",
              "| Pattern | Trigger: close above | Invalidation | Expires in |", "|---|---:|---:|---:|"]
        L += [f"| {x['pattern']} | {fmt_px(x['trigger_close_above'])} | {fmt_px(x['invalidation'])} | {x['expires_in_bars']} bars |"
              for x in p["pending_bullish"]]
        L.append("")
    if p.get("fresh_bearish"):
        L += ["⚠ **Bearish pattern just completed** — new long signals are blocked: " +
              ", ".join(f"{x['pattern']} ({x['bars_ago']} bars ago)" for x in p["fresh_bearish"]), ""]
    if p.get("recent_candles"):
        L += ["Candlesticks: " + "; ".join(f"{x['bars_ago']} bars ago: {', '.join(x['candles'])}" for x in p["recent_candles"]), ""]
    if p.get("bearish_research"):
        L += ["Bearish patterns on this coin's history (spot can't short — research + long blocker):", "",
              "| Pattern | Events | Short would have hit target first | Avg R (short, after costs) |", "|---|---:|---:|---:|"]
        L += [f"| {x['pattern']} | {x['events']} | {x['hit_rate']:.0%} | {x['avg_R_short']:+.2f} |" for x in p["bearish_research"]]
        L.append("")
    L += [f"_Not implemented yet: {', '.join(p.get('not_implemented', []))}._", ""]
    return L


def _gate_line(g: dict) -> str:
    return " ".join(("✅" if v else "❌") + k.split("_")[0] for k, v in g.items())


def _sizing_lines(p: dict) -> list:
    ex, k = p.get("execution") or {}, p.get("kelly")
    if not ex:
        return []
    L = [f"- At ${p['account_usd']:,.0f} and {p['risk_fraction_used']:.2%} risk: position ${ex['position_usd']:,.0f} "
         f"({ex['position_pct_of_account']}% of account, {ex['units']:g} units) · slippage at that size "
         f"{ex['slippage_bps_at_size']} bps ({ex['book_fraction']:.1%} of the ±1% book) → round-trip cost "
         f"{ex['round_trip_cost_pct']}% · "
         + ("backtest cost assumption holds" if ex["backtest_slippage_holds"] else
            f"**backtest slippage assumption does NOT hold** — keep orders under ${ex['max_order_usd_at_backtest_slippage']:,.0f} "
            "or work them as maker/limit orders"),
         f"- Volatility-target scale (2%/day target): {p['vol_target_scale']:.2f}x full size"]
    if k:
        L.append(f"- Kelly from {k['strategy']}'s {k['n']} walk-forward trades: full {k['full_kelly']:.2f}x capital, "
                 f"conservative (20th pct) {k['kelly_conservative']:.2f}x, half of that = {k['suggested_capital_fraction']:.2f}x "
                 f"→ {k['risk_fraction']:.2%} account risk per trade after the status cap")
    return L


def _markov_line(reg: dict) -> str:
    mv = reg.get("markov") or {}
    if not mv.get("available"):
        return "Markov-switching volatility filter: n/a."
    ago = mv.get("p_high_vol_5_bars_ago")
    ago_txt = "n/a" if ago is None else f"{ago:.0%}"
    return (f"Markov-switching volatility model (2 states, filtered = no lookahead): P(high-variance state now) "
            f"**{mv['p_high_vol']:.0%}** (5 bars ago {ago_txt}), "
            f"high state ≈ {mv['high_state_vol_ratio']}x the calm state's volatility, typical spell "
            f"{mv['expected_duration_bars']} bars, share of history {mv['smoothed_share_high']:.0%}"
            + (" — **treated as high volatility: dip / momentum / range modules paused.**" if reg.get("markov_high_vol") else "."))


def _overfit_line(r: dict) -> str:
    ov = r.get("overfit") or {}
    if not ov:
        return "- Overfitting statistics: n/a"
    mtrl = ov.get("min_track_record_years")
    return (f"- Overfitting: {ov.get('trials')} parameter sets tried · PBO {ov.get('pbo', 'n/a')} "
            f"(P(loss out-of-sample) {ov.get('p_loss_oos', 'n/a')}, IS→OOS slope {ov.get('degradation_slope', 'n/a')}) · "
            f"annual Sharpe {ov.get('sharpe_annual', 'n/a')} vs expected max of {ov.get('trials')} random trials "
            f"{ov.get('sr0_annual', 'n/a')} · PSR {ov.get('psr')} · DSR {ov.get('dsr')} · "
            f"FDR q {r.get('fdr_q', 'n/a')} · minimum track record "
            f"{'never significant' if mtrl is None else f'{mtrl} years'}")


def to_markdown(a: dict) -> str:
    p, reg = a["plan"], a["regime"]
    L = [f"# Coin audit: {a['symbol']} · {a['timeframe']}", "",
         f"**Verdict: {a['verdict']} — {a['score']}/100**", "", a["verdict_text"], ""]
    L += [f"> ⚠ {f}" for f in a["flags"]] + ([""] if a["flags"] else [])
    L += [f"Audited {a['audit_time_utc']} UTC · {a['candles']} {a['timeframe']} candles to {a['data_until_utc']} UTC · "
          f"{a['history_days']} days listed", "",
          "## Regime", "",
          f"Trend state **{reg['trend_state']}** ({reg['direction']}, ADX {reg['adx']}, efficiency {reg['efficiency_ratio']}) · "
          f"volatility **{reg['vol_state']}** ({reg['vol_percentile_1y']:.0%} percentile) · BTC **{reg['btc']}** · "
          f"liquidity **{reg['liquidity']}** · funding **{reg['funding']}**", "",
          _markov_line(reg), ""]
    L += market_md(a)
    L += patterns_md(a)
    L += ["## Scorecard", "", "| Check | Score | Status | Weight |", "|---|---:|---|---:|"]
    for r in a["checks"]:
        L.append(f"| {r['name']} | {r['score']:.0f} | {r['status'].upper()} | {WEIGHTS[r['name']] * r.get('weight_mult', 1.0):g} |")
    L += ["", "## Strategy library", "",
          "| Strategy | Evidence | Status | Gates | WF trades | Trades/yr | Win % | Avg R | WF PF | WF exp. | PF @2x cost | Holdout exp. | MC p | FDR q | PBO | DSR | Sharpe vs B&H | Signal now | Regime fit |",
          "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|"]
    for r in a["strategies"]:
        ov = r.get("overfit", {})
        L.append(f"| {r['name']} | {r['evidence']} | **{r['status']}** | {r['gates_passed']}/{val.N_GATES} | {r['wf']['trades']} | "
                 f"{r['benchmark']['trades_per_year']} | {r['wf']['win_rate']:.0%} | {r['wf']['avg_R']:+.2f} | "
                 f"{r['wf']['profit_factor']} | {r['wf']['expectancy_pct']:+.2f}% | {r['wf_cost2x']['profit_factor']} | "
                 f"{r['holdout']['expectancy_pct']:+.2f}% ({r['holdout']['trades']}) | {r['monte_carlo']['p_value']} | "
                 f"{r.get('fdr_q', '—')} | {ov.get('pbo', '—')} | {ov.get('dsr', '—')} | "
                 f"{r['benchmark']['strategy_sharpe']} vs {r['benchmark']['bh_sharpe']}{' ✓' if r['benchmark']['beats_bh_sharpe'] else ''} | "
                 f"{'YES' if r['signal_now'] else '—'} | {'yes' if r['regime_ok'] else 'no'} |")
    bb = a["strategies"][0]["benchmark"]
    beat = sum(r["benchmark"]["beats_bh_sharpe"] for r in a["strategies"])
    L += ["", f"Benchmarks over the same walk-forward window: **buy & hold {bb['bh_return_pct']:+.1f}%** "
          f"(Sharpe {bb['bh_sharpe']}), **no-trade 0%**. {beat} of {len(a['strategies'])} strategies beat buy & hold "
          f"on Sharpe (btc-strategy-lab found only 3 of 42 public strategies did)."]
    L += ["", "Gates: " + val.GATE_LEGEND + ". FDR q = Benjamini-Hochberg adjusted bootstrap p across the whole "
          "library; PBO = probability of backtest overfitting (CSCV); DSR = deflated Sharpe (probability the true "
          "Sharpe > 0 after the number of parameter sets tried, skew and kurtosis).", ""]
    for r in a["strategies"]:
        L += [f"### {r['name']} — {r['status']}", f"- {r['evidence_note']}",
              f"- Chosen parameters (past data only): `{r['params']}`",
              f"- {_gate_line(r['gates'])}",
              "- By regime at entry (development period): " + " · ".join(
                  f"{k} {v['trades']} trades, win {v['win_rate']:.0%}, PF {v['profit_factor']}, avg R {v['avg_R']:+.2f}"
                  for k, v in r["benchmark"].get("by_regime", {}).items()),
              f"- Fold returns: {r['fold_returns_pct']} · neighbours viable {r['neighbour_share']:.0%} · "
              f"expectancy without top {r['concentration']['top_trades_removed']} trades "
              f"{r['concentration']['expectancy_without_top_pct']:+.3f}% · Monte Carlo 95% drawdown "
              f"{r['monte_carlo']['dd_p95_pct']}%",
              _overfit_line(r)]
        if r.get("ablation"):
            L += ["", "| Module ablation | Median fold return | Trades | Keep |", "|---|---:|---:|---|"]
            L += [f"| {x['module']} | {x['median_fold_return_pct']:+.2f}% | {x['trades']} | {'yes' if x['keep'] else 'drop'} |"
                  for x in r["ablation"]]
        L.append("")
    L += ["## Signal records (standard interface — never orders)", ""]
    L += (["```json", json.dumps(a["signals"], indent=2), "```"] if a["signals"]
          else ["No strategy fired on the last closed candle."])
    L += ["", "## Research analyses (not executed)", ""]
    for x in a["research"]:
        L.append(f"### {x['name']} — {x.get('status', 'N/A')} (evidence {x.get('evidence', '-')})")
        L += [f"- {n}" for n in x["notes"]] + [""]
    L += ["## Trade plan (spot long, reference only)", "",
          f"- Price {fmt_px(p['price'])} · entry zone {fmt_px(p['entry_low'])} – {fmt_px(p['entry_high'])}",
          f"- Stop {fmt_px(p['stop'])} ({p['stop_distance_pct']}%) · T1 {fmt_px(p['target1'])} · T2 {fmt_px(p['target2'])}",
          f"- Resistance: 20-bar high {fmt_px(p['resistance_20_bars'])}, 90-day high {fmt_px(p['resistance_90d'])}",
          f"- Size for 1% account risk: {p['position_size_pct_of_account']}% · horizon {p['horizon']}"]
    L += _sizing_lines(p) + [""]
    if a["verdict"] != "FAVORABLE":
        L += ["_No approved signal — levels are for reference only._", ""]
    if a.get("track_record"):
        L += ["Live track record of the strategies above (from `--review` of logged signals): " + "; ".join(
            f"{k}: {v['live_win_rate']:.0%} of {v['n']} graded vs backtest {v['backtest_win_rate']:.0%}"
            + (" ⚠ drift" if v.get("drift") else "") for k, v in a["track_record"].items()), ""]
    q = a["best_quantstats"]
    L += [f"Best-validated strategy **{a['best_strategy']}**: Sharpe {q['sharpe']} (buy & hold {q['bh_sharpe']}), "
          f"Sortino {q['sortino']}, max drawdown {q['max_dd_pct']}% (B&H {q['bh_max_dd_pct']}%), "
          f"worst 30 days {q['worst_30d_pct']}%, time in market {q['exposure_pct']}%."]
    if a.get("tearsheet"):
        L.append(f"Tearsheet: `{a['tearsheet']}`")
    L += ["", "## Findings", ""]
    for r in a["checks"]:
        L.append(f"### {r['name']} — {r['score']:.0f} ({r['status'].upper()})")
        L += [f"- {n}" for n in r["notes"]] + [""]
    L += ["## Not included / never deployed", ""]
    L += [f"- **{k}** — {v}" for k, v in analyses.EXCLUDED]
    L += ["- Screenshot-only or 'AI accuracy' strategies, anything without a fee/slippage model, models tuned and "
          "evaluated on the same period, withdrawal-enabled keys.", "",
          "---", "Research output from public market data. Not financial advice."]
    return "\n".join(L)


def print_summary(a: dict):
    bar = "=" * 86
    print(bar)
    print(f" {a['symbol']} {a['timeframe']}  |  {a['verdict']}  |  score {a['score']}/100  |  "
          f"regime {a['regime']['trend_state']}/{a['regime']['vol_state']} vol/BTC {a['regime']['btc']}")
    print(bar)
    mr = a.get("market_regime")
    if mr:
        sc = mr["btc_score"] if a["base"] == "BTC" else mr["alt_score"]
        print(f"  Market: {mr['regime']} | {'BTC' if a['base'] == 'BTC' else 'alt'} score {sc:.0f}/100 | {mr['behaviour']}")
    pp = a.get("patterns") or {}
    for x in pp.get("pending_bullish", [])[:3]:
        print(f"  Forming: {x['pattern']} — needs close above {fmt_px(x['trigger_close_above'])}")
    for x in pp.get("fresh_bearish", []):
        print(f"  ! Bearish {x['pattern']} completed {x['bars_ago']} bars ago — longs blocked")
    for r in a["checks"]:
        print(f"  {r['name']:<36} {r['score']:>5.0f}  {r['status'].upper()}")
    print("  " + "-" * 74)
    for r in a["strategies"]:
        sig = "SIGNAL" if r["signal_now"] else ""
        print(f"  {r['name'][:52]:<52} {r['status']:<10} {r['gates_passed']:>2}/{val.N_GATES}  PF {r['wf']['profit_factor']:<5} {sig}")
    print("  " + "-" * 74)
    for x in a["research"]:
        print(f"  {x['name'][:52]:<52} {x.get('status', 'N/A')}")
    for f in a["flags"]:
        print(f"  ! {f}")
    for s in a["signals"]:
        extra = f", meta {s['meta_decision']} p={s['meta_p']:.0%}" if s.get("meta_p") is not None else ""
        print(f"  > {s['strategy_id']}: {s['decision']} (confidence {s['confidence']:.0%}{extra}, "
              f"risk {s['max_risk_fraction']:.2%})")
    p = a["plan"]
    print(f"\n  {a['verdict_text']}")
    print(f"  Entry {fmt_px(p['entry_low'])} – {fmt_px(p['entry_high'])} | stop {fmt_px(p['stop'])} | "
          f"T1 {fmt_px(p['target1'])} | T2 {fmt_px(p['target2'])}")
    print(bar)


def _json(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def save(a: dict, tearsheet: bool = True) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = a["audit_time_utc"].replace(" ", "_").replace(":", "")
    base = REPORTS / f"{a['symbol']}_{a['timeframe']}_{stamp}"
    k = 2
    while Path(str(base) + ".md").exists():                      # same minute twice (watch mode / replays)
        base = REPORTS / f"{a['symbol']}_{a['timeframe']}_{stamp}_r{k}"
        k += 1
    qret, bench, title = a.pop("_qs")
    ts = analytics.tearsheet(qret, bench, Path(str(base) + "_tearsheet.html"),
                             f"{a['symbol']} {a['timeframe']} — {title} vs buy & hold") if tearsheet else None
    if ts:
        a["tearsheet"] = ts.name
    a["report_path"] = Path(str(base) + ".md").name
    Path(str(base) + ".md").write_text(to_markdown(a), encoding="utf-8")
    Path(str(base) + ".json").write_text(json.dumps(a, indent=2, default=_json), encoding="utf-8")
    p = a["plan"]
    lead_days = p["horizon"].split("~")[1].split(" ")[0] if "~" in p["horizon"] else "10"
    row = {"audit_time_utc": a["audit_time_utc"], "symbol": a["symbol"], "timeframe": a["timeframe"],
           "price": p["price"], "score": a["score"], "verdict": a["verdict"],
           **{k: p[k] for k in ("entry_low", "entry_high", "stop", "target1", "target2")},
           "horizon_days": max(1.0, float(lead_days)),
           "accepted": sum(r["status"] == "ACCEPTED" for r in a["strategies"]),
           "candidates": sum(r["status"] == "CANDIDATE" for r in a["strategies"]),
           "approved_signals": sum(s["decision"] == "APPROVED" for s in a["signals"]),
           "outcome": "", "outcome_return_pct": "", "outcome_checked_utc": ""}
    _append(LOG, LOG_FIELDS, [row])
    _append(SIGNAL_LOG, SIGNAL_FIELDS, a["signals"])
    return Path(str(base) + ".md")


def _append(path: Path, fields: list, rows: list):
    if not rows:
        return
    if path.exists():                                             # older file with fewer columns -> migrate
        with path.open(encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
        if header and header != fields:
            old = list(csv.DictReader(path.open(encoding="utf-8")))
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                w.writeheader(); w.writerows(old)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------- review
def review(client, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if not LOG.exists():
        print("No audits logged yet."); review_signals(client, now); return
    rows = list(csv.DictReader(LOG.open(encoding="utf-8")))
    updated = 0
    for r in rows:
        if r.get("outcome"):
            continue
        t0 = datetime.strptime(r["audit_time_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        age = (now - t0).total_seconds() / 86400
        hz = float(r.get("horizon_days") or 10)
        if age < 1:
            continue
        h1 = client.klines(r["symbol"], "1h", int(24 * (hz + 1)), start_ms=int(t0.timestamp() * 1000))
        if h1.empty:
            continue
        stop, t1 = float(r["stop"]), float(r["target1"])
        mid = (float(r["entry_low"]) + float(r["entry_high"])) / 2
        outcome = px = None
        for _, b in h1.iterrows():
            if b["low"] <= stop:
                outcome, px = "stop", stop; break
            if b["high"] >= t1:
                outcome, px = "target1", t1; break
        if outcome is None:
            if age < hz:
                continue
            outcome, px = "timeout", float(h1["close"].iloc[-1])
        r.update({"outcome": outcome, "outcome_return_pct": round((px / mid - 1) * 100, 2),
                  "outcome_checked_utc": now.strftime("%Y-%m-%d %H:%M")})
        updated += 1
    with LOG.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"Updated {updated} audit outcome(s).")
    done = [r for r in rows if r.get("outcome")]
    if done:
        d = pd.DataFrame(done)
        d["outcome_return_pct"] = d["outcome_return_pct"].astype(float)
        print("\nOutcomes by verdict and timeframe:")
        print(d.groupby(["verdict", "timeframe"])["outcome_return_pct"].agg(["count", "mean"]).round(2).to_string())
    review_signals(client, now)


def review_signals(client, now: datetime | None = None) -> dict:
    """Grade every logged strategy signal (target / stop / timeout from the next candle's open) and
    compare each strategy's LIVE hit rate with its backtest win rate (one-sided binomial test).
    Writes logs/track_record.json, which later audits read: a strategy whose live results are
    significantly worse than its backtest gets its new signals BLOCKED (the feedback loop)."""
    if not SIGNAL_LOG.exists():
        return {}
    now = now or datetime.now(timezone.utc)
    rows = list(csv.DictReader(SIGNAL_LOG.open(encoding="utf-8")))
    updated = 0
    for r in rows:
        if r.get("outcome") or not r.get("price") or not r.get("stop_px"):
            continue
        try:
            t0 = pd.Timestamp(r["timestamp"])
            t0 = t0.tz_localize("UTC") if t0.tzinfo is None else t0.tz_convert("UTC")
            stop, tgt = float(r["stop_px"]), float(r["target_px"])
            bars = int(float(r["expected_holding_bars"] or 30))
        except (ValueError, KeyError):
            continue
        hours = max(24, int(bars * rg.TF_MINUTES.get(r["timeframe"], 240) / 60))
        if (now - t0.to_pydatetime()).total_seconds() < 3600:
            continue
        sym = r["symbol"].replace("/", "")
        try:
            h1 = client.klines(sym, "1h", hours + 1, start_ms=int(t0.value // 1_000_000))
        except BinanceError:
            continue
        if h1.empty:
            continue
        entry = float(h1["open"].iloc[0])
        outcome = px = None
        for _, b in h1.iterrows():
            if b["low"] <= stop:
                outcome, px = "stop", stop; break
            if b["high"] >= tgt:
                outcome, px = "target", tgt; break
        if outcome is None:
            if len(h1) < hours:
                continue                                          # horizon not over yet
            outcome, px = "timeout", float(h1["close"].iloc[-1])
        r.update({"outcome": outcome, "outcome_return_pct": round((px / entry - 1) * 100 - 2 * val.COST * 100, 3),
                  "outcome_checked_utc": now.strftime("%Y-%m-%d %H:%M")})
        updated += 1
    with SIGNAL_LOG.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SIGNAL_FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    track = track_record(rows, now)
    TRACK_RECORD.parent.mkdir(parents=True, exist_ok=True)
    TRACK_RECORD.write_text(json.dumps(track, indent=2), encoding="utf-8")
    print(f"Graded {updated} new signal(s); track record for {len(track)} strategies -> {TRACK_RECORD.name}")
    for k, v in sorted(track.items()):
        print(f"  {k:<30} live {v['live_win_rate']:.0%} of {v['n']} (avg {v['avg_return_pct']:+.2f}%) vs backtest "
              f"{v['backtest_win_rate']:.0%}  p={v['p_value']}{'  DRIFT' if v['drift'] else ''}")
    return track


def track_record(rows: list[dict], now: datetime) -> dict:
    from scipy.stats import binomtest
    out = {}
    graded = [r for r in rows if r.get("outcome") and r.get("outcome_return_pct") not in ("", None)]
    for sid in sorted({r["strategy_id"] for r in graded}):
        g = [r for r in graded if r["strategy_id"] == sid]
        rets = np.array([float(r["outcome_return_pct"]) for r in g])
        wins = int((rets > 0).sum())
        bt = [float(r["wf_win_rate"]) for r in g if r.get("wf_win_rate") not in ("", None)]
        bt_rate = float(np.mean(bt)) if bt else 0.5
        p = float(binomtest(wins, len(g), max(min(bt_rate, 0.999), 0.001), alternative="less").pvalue) if len(g) else 1.0
        out[sid] = {"n": len(g), "wins": wins, "live_win_rate": round(wins / len(g), 3), "backtest_win_rate": round(bt_rate, 3),
                    "avg_return_pct": round(float(rets.mean()), 3), "p_value": round(p, 3),
                    "drift": bool(len(g) >= 8 and p < 0.05), "updated": now.strftime("%Y-%m-%d %H:%M")}
    return out


def watch(base: str, quote: str):
    if not FT_CONFIG.exists():
        print(f"  (Freqtrade config not found at {FT_CONFIG})"); return
    cfg = json.loads(FT_CONFIG.read_text(encoding="utf-8"))
    pair, wl = f"{base}/{quote}", cfg["exchange"]["pair_whitelist"]
    if pair in wl:
        print(f"  {pair} is already on the Freqtrade whitelist."); return
    wl.append(pair)
    FT_CONFIG.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    print(f"  Added {pair} to the Freqtrade whitelist (restart the dry-run bots to pick it up).")


# --------------------------------------------------------------------- main
def make_client(args):
    """Build (client, futures client, now) from the command line: live, --offline, --demo or --as-of."""
    if args.demo:
        if not (DEMO_DIR / "DEMOUSDT_15m.parquet").exists():
            import synthetic
            print("  writing the synthetic demo universe (once) ...", flush=True)
            synthetic.write_universe(DEMO_DIR, {"BTCUSDT": {}, "ETHUSDT": {"price": 2500.0, "base_volume": 2000.0},
                                                "DEMOUSDT": {"price": 3.2, "base_volume": 30_000.0}}, days=760, seed=5)
        args.offline = str(DEMO_DIR)
        if not args.coins and not args.review:
            args.coins = ["DEMO"]
        args.tf = args.tf or "4h"
    if args.offline:
        client = OfflineClient(args.offline, as_of=args.as_of, quote=args.quote.upper())
        fut = None if args.no_futures else OfflineFuturesClient(args.offline, as_of=args.as_of)
        now = datetime.fromtimestamp(to_ms(args.as_of) / 1000, tz=timezone.utc) if args.as_of else "data"
        return client, fut, now
    client = BinanceClient(cache_dir=None if args.no_cache else KLINE_CACHE, as_of=args.as_of)
    fut = None
    if not args.no_futures:
        try:
            fut = FuturesClient()
        except Exception:
            fut = None
    now = datetime.fromtimestamp(to_ms(args.as_of) / 1000, tz=timezone.utc) if args.as_of else None
    return client, fut, now


def main(argv=None):
    ap = argparse.ArgumentParser(description="Audit a Binance Spot coin with a validated strategy library.")
    ap.add_argument("coins", nargs="*")
    ap.add_argument("--tf", help="15m | 1h | 4h | 1d | all (asked interactively if omitted)")
    ap.add_argument("--quote", default="USDT")
    ap.add_argument("--no-ml", action="store_true")
    ap.add_argument("--no-futures", action="store_true", help="skip the funding-carry lookup")
    ap.add_argument("--no-market", action="store_true", help="skip the crypto-wide + macro regime layer")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--offline", metavar="DIR", help="serve candles from local parquet files (no network)")
    ap.add_argument("--as-of", dest="as_of", help="point-in-time replay, e.g. 2026-06-01 or 2026-06-01T12:00Z")
    ap.add_argument("--demo", action="store_true", help="offline run on synthetic data (self-test of the install)")
    ap.add_argument("--strategies", help="comma-separated ids / families / name fragments to restrict the library")
    ap.add_argument("--no-cache", action="store_true", help="do not keep a local kline cache")
    ap.add_argument("--scan", nargs="?", const=CFG["scan"]["universe"], type=int, metavar="N",
                    help="audit the N most traded coins, rank them and suggest a correlation-aware allocation")
    ap.add_argument("--jobs", type=int, default=CFG["scan"]["jobs"], help="parallel processes for --scan (0 = all cores)")
    ap.add_argument("--loop", action="store_true", help="watch mode: re-run at every candle close until Ctrl-C")
    ap.add_argument("--loop-max", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--notify", action="store_true", help="push approved signals (DISCORD_WEBHOOK_URL / TELEGRAM_*)")
    ap.add_argument("--dashboard", action="store_true", help="rebuild audit/reports/dashboard.html")
    ap.add_argument("--no-tearsheet", action="store_true", help="skip the quantstats HTML tearsheet (faster)")
    args = ap.parse_args(argv)
    client, fut, now = make_client(args)
    if args.review:
        review(client, now if isinstance(now, datetime) else None)
        if args.dashboard:
            print(f"  Dashboard: {dashboard.build(REPORTS, LOG, SIGNAL_LOG)}")
        return 0
    if not args.coins and args.scan is None:
        if args.dashboard:
            print(f"  Dashboard: {dashboard.build(REPORTS, LOG, SIGNAL_LOG)}"); return 0
        ap.print_help(); return 1
    tf = (args.tf or (ask_timeframe() if sys.stdin.isatty() else "4h")).lower()
    if tf not in TIMEFRAMES + ["all"]:
        print(f"Unknown timeframe {tf}; use one of {TIMEFRAMES} or all"); return 1
    tfs = TIMEFRAMES if tf == "all" else [tf]
    strategies = [x.strip() for x in args.strategies.split(",")] if args.strategies else None
    use_market = not args.no_market and not (args.offline or args.demo)

    def once() -> int:
        """0 = everything worked; 2 = at least one audit (or the scan) reported an error."""
        if args.scan is not None:
            return 0 if scan(args, tfs[0], now, strategies, use_market, client, fut) else 2
        return 2 if run_audits(client, fut, args, tfs, now, strategies, use_market).errors else 0

    rc = 0
    if args.loop:
        watch_loop(once, tfs[0], args.loop_max)
    else:
        rc = once()
    if args.dashboard or args.loop:
        print(f"  Dashboard: {dashboard.build(REPORTS, LOG, SIGNAL_LOG)}")
    print(f"\nLogs: {LOG} · {SIGNAL_LOG}")
    return rc


class Ranking(list):
    """The comparison rows of run_audits(); `.errors` counts audits that reported an error."""
    errors = 0


def run_audits(client, fut, args, tfs, now, strategies, use_market) -> Ranking:
    ranking = Ranking()
    for coin in args.coins:
        for t in tfs:
            print(f"\nAuditing {coin.upper()} on {t} ...")
            try:
                a = audit(client, fut, coin, args.quote.upper(), t, not args.no_ml, use_market, now=now,
                          strategies=strategies)
            except BinanceError as e:
                print(f"  ERROR: {e}")
                ranking.errors += 1
                continue
            path = save(a, tearsheet=not args.no_tearsheet and not args.loop)
            print_summary(a)
            print(f"  Report: {path}")
            if args.notify and notify.should_notify(a["verdict"]):
                sent = notify.send(notify.signal_message(a))
                print(f"  Notified: {', '.join(sent) or 'nobody'}")
            acc = [r["id"] for r in a["strategies"] if r["status"] == "ACCEPTED"]
            ok = [r for r in a["strategies"] if r["status"] != "REJECTED"]
            best_ok = max(ok, key=lambda r: r["wf"]["expectancy_pct"]) if ok else None
            ranking.append((a["score"], a["symbol"], t, a["verdict"], len(acc),
                            f"{best_ok['id']} {best_ok['wf']['expectancy_pct']:+.3f}%/trade "
                            f"({best_ok['wf']['trades']} trades, {best_ok['status']})" if best_ok else "none passed"))
        if args.watch and ranking:
            watch(ranking[-1][1][: -len(args.quote)], args.quote.upper())
    tf = args.tf
    if len(ranking) > 1:
        print("\nComparison (best first):")
        print(f"  {'symbol':<12} {'tf':<4} {'score':>6}  {'verdict':<10} {'accepted':>8}  best validated strategy")
        for s, sym, t, v, na, ex in sorted(ranking, key=lambda x: (x[4], x[0]), reverse=True):
            print(f"  {sym:<12} {t:<4} {s:>6}  {v:<10} {na:>8}  {ex}")
        if tf == "all":
            for sym in dict.fromkeys(r[1] for r in ranking):
                mine = [r for r in ranking if r[1] == sym]
                top = max(mine, key=lambda x: (x[4], x[5] != "none passed", x[0]))
                if top[4] == 0 and top[5] == "none passed":
                    print(f"\n  {sym}: no timeframe produced a strategy that survived validation — "
                          f"no timeframe is recommended (highest score {top[0]} on {top[2]}).")
                else:
                    print(f"\n  Best timeframe for {sym}: {top[2]} ({top[4]} accepted, score {top[0]}; {top[5]}).")
    return ranking


# ------------------------------------------------------------------ scan / loop (v6)
def client_spec(args) -> dict:
    return {"offline": args.offline, "as_of": args.as_of, "quote": args.quote.upper(),
            "no_futures": args.no_futures, "no_cache": args.no_cache}


def client_from_spec(spec: dict):
    ns = argparse.Namespace(demo=False, review=False, coins=[], tf=None, **spec)
    return make_client(ns)


def _scan_worker(spec, coin, quote, tf, use_ml, use_market, now, strategies):
    client, fut, now2 = client_from_spec(spec)
    now = now if now is not None else now2
    try:
        a = audit(client, fut, coin, quote, tf, use_ml, use_market, now=now, strategies=strategies)
    except BinanceError as e:
        return {"error": str(e), "symbol": coin}
    save(a, tearsheet=False)                                     # scans skip the slow HTML tearsheets
    d1 = client.klines(a["symbol"], "1d", 120)["close"]
    return {"summary": portfolio.summarize(a), "closes": (d1.index.astype("int64").tolist(), d1.values.tolist()),
            "message": notify.signal_message(a) if notify.should_notify(a["verdict"]) else None}


def scan(args, tf, now, strategies, use_market, client, fut) -> Path | None:
    quote = args.quote.upper()
    try:
        syms = portfolio.universe(client, quote, args.scan)
    except BinanceError as e:
        print(f"  ERROR: {e}"); return None
    syms = [s for s in syms if s.endswith(quote)]
    if not syms:
        print("  No symbols in the universe."); return None
    print(f"\nScanning {len(syms)} coins on {tf}: {', '.join(s[:-len(quote)] for s in syms)}")
    jobs = args.jobs if args.jobs > 0 else None
    tasks = [(client_spec(args), s[:-len(quote)], quote, tf, not args.no_ml, use_market, now, strategies) for s in syms]
    results = []
    if jobs == 1 or len(syms) == 1:
        for t in tasks:
            print(f"  {t[1]} ...", flush=True)
            results.append(_scan_worker(*t))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            for t, res in zip(tasks, ex.map(_scan_worker, *zip(*tasks))):
                print(f"  {t[1]} done", flush=True)
                results.append(res)
    rows, closes, messages = [], {}, []
    for r in results:
        if "error" in r:
            print(f"  {r['symbol']}: {r['error']}"); continue
        rows.append(r["summary"])
        idx, vals = r["closes"]
        closes[r["summary"]["symbol"]] = pd.Series(vals, index=pd.to_datetime(idx, utc=True))
        if r.get("message"):
            messages.append(r["message"])
    if not rows:
        print("  Nothing audited."); return None
    corr, cs = portfolio.correlation(closes)
    rows = portfolio.cross_section(portfolio.rank(rows))
    alloc = portfolio.allocate(rows, corr, cs)
    stamp = (now if isinstance(now, datetime) else datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    note = (f"Universe: top {len(syms)} {quote} pairs by 24h volume (min ${CFG['scan']['min_volume_usd'] / 1e6:g}M), "
            f"leveraged tokens and stablecoins excluded; every coin ran the full audit pipeline.")
    REPORTS.mkdir(parents=True, exist_ok=True)
    base = REPORTS / f"scan_{tf}_{stamp.replace(' ', '_').replace(':', '')}"
    Path(str(base) + ".md").write_text(portfolio.to_markdown(rows, alloc, tf, stamp, note), encoding="utf-8")
    Path(str(base) + ".json").write_text(json.dumps({"rows": rows, "allocation": alloc, "correlation_symbols": cs,
                                                      "correlation": np.nan_to_num(corr).round(3).tolist()},
                                                     indent=2, default=_json), encoding="utf-8")
    print(f"\n  {'#':>2} {'symbol':<12} {'verdict':<10} {'score':>5} {'acc':>3} {'approved':<28} best validated")
    for i, r in enumerate(rows, 1):
        best = f"{r['best_strategy']} {r['best_expectancy_pct']:+.2f}%" if r["best_strategy"] else "none"
        print(f"  {i:>2} {r['symbol']:<12} {r['verdict']:<10} {r['score']:>5} {r['accepted']:>3} "
              f"{', '.join(r['approved'])[:28]:<28} {best}")
    if alloc:
        print("\n  Allocation: " + "; ".join(f"{o['symbol']} {o['allocated_risk']:.2%}" for o in alloc if o["allocated"]))
    print(f"  Scan report: {base}.md")
    if args.notify and messages:
        notify.send("\n\n".join(messages)[:3800])
    return Path(str(base) + ".md")


def next_close_seconds(tf: str, now_ts: float | None = None, grace: int = 30) -> float:
    """Seconds until the next candle of `tf` closes (UTC-aligned) plus a grace period for the feed."""
    now_ts = time.time() if now_ts is None else now_ts
    step = rg.TF_MINUTES[tf] * 60
    return (int(now_ts // step) + 1) * step - now_ts + grace


def watch_loop(run_once, tf: str, max_iter: int = 0, sleep=None):
    sleep = sleep or time.sleep
    i = 0
    while True:
        run_once()
        i += 1
        if max_iter and i >= max_iter:
            return
        wait = next_close_seconds(tf)
        print(f"\n  Watch mode: next run in {wait / 60:.1f} min (after the next {tf} candle closes). Ctrl-C to stop.")
        try:
            sleep(wait)
        except KeyboardInterrupt:
            print("  stopped."); return


if __name__ == "__main__":
    sys.exit(main())
