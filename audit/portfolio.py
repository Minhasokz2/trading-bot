"""Market scanner support (v6): universe selection, ranking and correlation-aware allocation.

The scanner audits many coins with the same pipeline, then treats the approved signals as a
portfolio: coins whose daily returns correlate above `cluster_corr` share a cluster, each cluster
gets at most `max_per_cluster` positions, total open risk stays under `portfolio_heat`. Nothing is
executed — the allocation is a suggestion in the scan report.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import sizing
from binance_client import is_tradable_symbol
from settings import CFG


def universe(client, quote: str = "USDT", n: int | None = None, min_volume: float | None = None,
             exclude: tuple = ()) -> list[str]:
    """Top-n symbols by 24h quote volume that are plain spot pairs (no leveraged tokens, no stablecoins)."""
    n = CFG["scan"]["universe"] if n is None else n
    min_volume = CFG["scan"]["min_volume_usd"] if min_volume is None else min_volume
    rows = []
    for t in client.ticker_24h_all():
        sym = t.get("symbol", "")
        if not is_tradable_symbol(sym, quote) or sym in exclude:
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        if qv >= min_volume:
            rows.append((qv, sym))
    rows.sort(reverse=True)
    return [s for _, s in rows[:n]]


def summarize(a: dict) -> dict:
    """The few fields of an audit the scan table needs."""
    ok = [r for r in a["strategies"] if r["status"] != "REJECTED"]
    best = max(ok, key=lambda r: r["wf"]["expectancy_pct"]) if ok else None
    approved = [s for s in a["signals"] if s["decision"] == "APPROVED"]
    mr = a.get("market_regime") or {}
    risk = max([s["max_risk_fraction"] for s in approved], default=0.0)
    lead = next((r for r in a["strategies"] if approved and r["id"] == approved[0]["strategy_id"]), None)
    return {"symbol": a["symbol"], "base": a["base"], "timeframe": a["timeframe"], "score": a["score"],
            "verdict": a["verdict"], "accepted": sum(r["status"] == "ACCEPTED" for r in a["strategies"]),
            "candidates": sum(r["status"] == "CANDIDATE" for r in a["strategies"]),
            "approved": [s["strategy_id"] for s in approved], "risk_fraction": risk,
            "best_strategy": best["id"] if best else None,
            "best_expectancy_pct": best["wf"]["expectancy_pct"] if best else None,
            "best_status": best["status"] if best else None,
            "alt_score": mr.get("btc_score" if a["base"] == "BTC" else "alt_score"),
            "regime": a["regime"]["trend_state"], "vol": a["regime"]["vol_state"],
            "stop_pct": a["plan"]["stop_distance_pct"], "price": a["plan"]["price"],
            "rs_30d_vs_btc": (a["checks"][4]["metrics"].get("ret_30d_pct", 0) or 0)
            - (a["checks"][4]["metrics"].get("btc_ret_30d_pct", 0) or 0),
            "flags": a.get("flags", []), "report": a.get("report_path", ""),
            "lead_horizon_bars": lead["horizon_bars"] if lead else None}


def rank(rows: list[dict]) -> list[dict]:
    """Approved signals first, then validated edge, then the audit score."""
    return sorted(rows, key=lambda r: (len(r["approved"]) > 0, r["accepted"], r["score"],
                                       r["best_expectancy_pct"] or -99), reverse=True)


def correlation(closes: dict[str, pd.Series], days: int = 90) -> tuple[np.ndarray, list[str]]:
    """Pairwise correlation of daily log returns over the last `days` days."""
    syms = list(closes)
    if not syms:
        return np.zeros((0, 0)), syms
    df = pd.DataFrame({s: np.log(c.astype(float)).diff() for s, c in closes.items()}).tail(days)
    return df.corr().reindex(index=syms, columns=syms).values, syms


def allocate(rows: list[dict], corr: np.ndarray, syms: list[str]) -> list[dict]:
    """Greedy risk allocation across the ranked approved signals with cluster caps."""
    clusters = sizing.clusters_from_correlation(corr, syms) if len(syms) else {}
    cands = [{"symbol": r["symbol"], "risk_fraction": r["risk_fraction"], "verdict": r["verdict"],
              "approved": r["approved"], "stop_pct": r["stop_pct"]} for r in rows if r["approved"]]
    out = sizing.portfolio_heat(cands, clusters)
    acct = CFG["risk"]["account_usd"]
    for o in out:
        o["position_pct_of_account"] = round(o["allocated_risk"] / (o["stop_pct"] / 100) * 100, 2) if o["stop_pct"] else 0.0
        o["position_usd"] = round(acct * o["position_pct_of_account"] / 100, 2)
    return out


def cross_section(rows: list[dict]) -> list[dict]:
    """Relative-strength percentile of each coin's 30-day return vs BTC within the scanned universe."""
    vals = np.array([r["rs_30d_vs_btc"] for r in rows], float)
    n = len(vals)
    for r in rows:
        r["rs_percentile"] = round(float((vals < r["rs_30d_vs_btc"]).sum() / (n - 1)), 2) if n > 1 else 0.5
    return rows


def to_markdown(rows: list[dict], alloc: list[dict], tf: str, stamp: str, universe_note: str) -> str:
    L = [f"# Market scan · {tf} · {stamp} UTC", "", universe_note, "",
         "| # | Symbol | Verdict | Score | Accepted | Cand. | Approved signals | Best validated | RS 30d vs BTC | RS pct | Alt score | Regime | Flags |",
         "|---:|---|---|---:|---:|---:|---|---|---:|---:|---:|---|---|"]
    for i, r in enumerate(rows, 1):
        best = (f"{r['best_strategy']} {r.get('best_expectancy_pct') or 0:+.2f}%/trade ({r.get('best_status', '')})"
                if r.get("best_strategy") else "none")
        alt = "—" if r.get("alt_score") is None else f"{r['alt_score']:.0f}"
        L.append(f"| {i} | {r['symbol']} | **{r['verdict']}** | {r['score']} | {r['accepted']} | {r.get('candidates', 0)} | "
                 f"{', '.join(r['approved']) or '—'} | {best} | {r.get('rs_30d_vs_btc', 0):+.1f}% | {r.get('rs_percentile', 0.5):.0%} | "
                 f"{alt} | {r.get('regime', '?')}/{r.get('vol', '?')} vol | {'; '.join(f[:40] for f in r.get('flags', [])[:2])} |")
    L += ["", "## Suggested allocation (approved signals only, research — nothing is executed)", ""]
    if not alloc:
        L.append("_No approved signal in the scanned universe._")
    else:
        R = CFG["risk"]
        L += [f"Portfolio heat cap {R['portfolio_heat']:.2%} · max {R['max_positions']} positions · "
              f"max {R['max_per_cluster']} per correlation cluster (ρ ≥ {R['cluster_corr']}) · account ${R['account_usd']:,.0f}", "",
              "| Symbol | Allocated | Risk / trade | Position % | Position $ | Cluster | Signals | Why not |",
              "|---|---|---:|---:|---:|---:|---|---|"]
        L += [f"| {o['symbol']} | {'yes' if o['allocated'] else 'no'} | {o['allocated_risk']:.2%} | "
              f"{o['position_pct_of_account'] if o['allocated'] else 0}% | {o['position_usd'] if o['allocated'] else 0:,.0f} | "
              f"{o['cluster']} | {', '.join(o['approved'])} | {o['reason']} |" for o in alloc]
        L.append(f"\nTotal open risk if all filled: {sum(o['allocated_risk'] for o in alloc):.2%} of the account.")
    L += ["", "---", "Research output from public market data. Not financial advice."]
    return "\n".join(L)
