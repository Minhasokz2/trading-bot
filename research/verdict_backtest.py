"""Audit the auditor: replay the WHOLE audit pipeline at many past dates and grade what it said.

At every as-of date the audit sees only candles that had closed by then (point-in-time), so this is
a backtest of the verdicts, scores and approved signals themselves - not of a single strategy.
Every verdict is then graded with the data that came AFTER it: did price reach target 1 before the
stop within the plan's horizon (or where did it close at timeout)?

Usage (from the project root):
    python research/verdict_backtest.py SOL                       # 4h, last 180 days, every 7 days
    python research/verdict_backtest.py SOL --tf 1d --span 365 --every 14 --jobs 4
    python research/verdict_backtest.py DEMO --offline audit/cache/demo --no-ml --strategies trend,dip
Output: research/results/verdict_backtest_<SYMBOL>_<tf>.csv and a summary
(hit rate and mean return by verdict, score-decile calibration, approved-signal outcomes by strategy).
"""
from __future__ import annotations

import argparse
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "audit"))
warnings.filterwarnings("ignore")

import audit as au  # noqa: E402
import regime as rg  # noqa: E402
from binance_client import BinanceClient, BinanceError, FuturesClient, OfflineClient, OfflineFuturesClient  # noqa: E402

RESULTS = ROOT / "results"


def _clients(offline: str | None, as_of, no_futures: bool):
    if offline:
        return OfflineClient(offline, as_of=as_of), (None if no_futures else OfflineFuturesClient(offline, as_of=as_of))
    fut = None
    if not no_futures:
        try:
            fut = FuturesClient()
        except Exception:
            fut = None
    return BinanceClient(cache_dir=au.KLINE_CACHE, as_of=as_of), fut


def replay_one(args_d: dict) -> dict:
    """One point-in-time audit + its grade (runs in a worker process)."""
    d = args_d["as_of"]
    client, fut = _clients(args_d["offline"], d, args_d["no_futures"])
    now = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
    row = {"as_of": d, "symbol": args_d["coin"], "timeframe": args_d["tf"]}
    try:
        a = au.audit(client, fut, args_d["coin"], args_d["quote"], args_d["tf"], not args_d["no_ml"], False, now=now,
                     strategies=args_d["strategies"])
    except BinanceError as e:
        row["error"] = str(e)
        return row
    p = a["plan"]
    approved = [s["strategy_id"] for s in a["signals"] if s["decision"] == "APPROVED"]
    row.update({"verdict": a["verdict"], "score": a["score"], "regime": a["regime"]["trend_state"],
                "vol": a["regime"]["vol_state"], "btc": a["regime"]["btc"],
                "accepted": sum(r["status"] == "ACCEPTED" for r in a["strategies"]),
                "candidates": sum(r["status"] == "CANDIDATE" for r in a["strategies"]),
                "approved": ";".join(approved), "price": p["price"], "entry": (p["entry_low"] + p["entry_high"]) / 2,
                "stop": p["stop"], "target1": p["target1"], "horizon_days": float(p["horizon"].split("~")[1].split(" ")[0])
                if "~" in p["horizon"] else 10.0, "flags": " | ".join(a["flags"])[:200]})
    # grade with the future
    future, _ = _clients(args_d["offline"], None, True)
    hours = int(row["horizon_days"] * 24) + 1
    try:
        h1 = future.klines(a["symbol"], "1h", hours, start_ms=int(now.timestamp() * 1000))
    except BinanceError:
        h1 = pd.DataFrame()
    outcome, px = "", None
    if not h1.empty:
        entry = row["entry"]
        for _, b in h1.iterrows():
            if b["low"] <= row["stop"]:
                outcome, px = "stop", row["stop"]; break
            if b["high"] >= row["target1"]:
                outcome, px = "target1", row["target1"]; break
        if not outcome:
            outcome, px = ("timeout", float(h1["close"].iloc[-1])) if len(h1) >= hours - 1 else ("pending", None)
        row["outcome"] = outcome
        row["return_pct"] = None if px is None else round((px / entry - 1) * 100, 3)
        row["buy_hold_pct"] = round((float(h1["close"].iloc[-1]) / float(h1["open"].iloc[0]) - 1) * 100, 3)
    return row


def summarize(df: pd.DataFrame) -> str:
    L = []
    g = df[df["outcome"].isin(["target1", "stop", "timeout"])]
    if g.empty:
        return "No graded verdicts yet."
    by = g.groupby("verdict").agg(n=("return_pct", "size"), hit_target=("outcome", lambda s: (s == "target1").mean()),
                                  stopped=("outcome", lambda s: (s == "stop").mean()), mean_ret=("return_pct", "mean"),
                                  bh=("buy_hold_pct", "mean")).round(3)
    L += ["Outcome by verdict (target 1 vs stop within the plan's horizon; return at exit, buy & hold same window):",
          by.to_string(), ""]
    g = g.assign(decile=pd.qcut(g["score"], q=min(5, g["score"].nunique()), duplicates="drop"))
    cal = g.groupby("decile", observed=True).agg(n=("return_pct", "size"), positive=("return_pct", lambda s: (s > 0).mean()),
                                                 mean_ret=("return_pct", "mean")).round(3)
    L += ["Score calibration (higher score should mean better outcomes):", cal.to_string(), ""]
    sig = g[g["approved"] != ""]
    if len(sig):
        rows = []
        for _, r in sig.iterrows():
            for s in r["approved"].split(";"):
                rows.append({"strategy": s, "return_pct": r["return_pct"], "hit": r["outcome"] == "target1"})
        s = pd.DataFrame(rows).groupby("strategy").agg(n=("hit", "size"), hit_target=("hit", "mean"),
                                                        mean_ret=("return_pct", "mean")).round(3)
        L += ["Approved signals by strategy:", s.to_string(), ""]
    else:
        L.append("No approved signal on any replay date.")
    fav = g[g["verdict"] == "FAVORABLE"]
    L.append(f"FAVORABLE verdicts: {len(fav)} of {len(g)}; positive {((fav['return_pct'] > 0).mean() if len(fav) else 0):.0%}; "
             f"all verdicts positive {((g['return_pct'] > 0).mean()):.0%}; buy & hold positive {((g['buy_hold_pct'] > 0).mean()):.0%}.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("coin")
    ap.add_argument("--quote", default="USDT")
    ap.add_argument("--tf", default="4h")
    ap.add_argument("--span", type=int, default=180, help="days back from the end of the data")
    ap.add_argument("--every", type=int, default=7, help="days between replay dates")
    ap.add_argument("--end", help="last as-of date (default: now, or the end of the offline data)")
    ap.add_argument("--offline", help="parquet directory (research/data/clean, audit/cache/demo ...)")
    ap.add_argument("--no-ml", action="store_true")
    ap.add_argument("--no-futures", action="store_true")
    ap.add_argument("--strategies")
    ap.add_argument("--jobs", type=int, default=1)
    a = ap.parse_args(argv)
    coin = a.coin.upper()
    if a.end:
        end = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)
    elif a.offline:
        c = OfflineClient(a.offline)
        end = c.klines(coin + a.quote.upper() if not coin.endswith(a.quote.upper()) else coin, "1h", 2).index[-1].to_pydatetime()
    else:
        end = datetime.now(timezone.utc)
    horizon_guard = timedelta(days=15)
    dates = []
    d = end - horizon_guard
    while d >= end - timedelta(days=a.span):
        dates.append(d.replace(minute=0, second=0, microsecond=0))
        d -= timedelta(days=a.every)
    dates = sorted(dates)
    strategies = [x.strip() for x in a.strategies.split(",")] if a.strategies else None
    tasks = [{"as_of": dt.strftime("%Y-%m-%dT%H:%M:%S"), "coin": coin, "quote": a.quote.upper(), "tf": a.tf,
              "offline": a.offline, "no_ml": a.no_ml, "no_futures": a.no_futures, "strategies": strategies} for dt in dates]
    print(f"Replaying {len(tasks)} audits of {coin} on {a.tf} from {dates[0]:%Y-%m-%d} to {dates[-1]:%Y-%m-%d} "
          f"({'offline' if a.offline else 'live, cached'}; {a.jobs} process(es)) ...")
    if a.jobs > 1:
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            rows = list(ex.map(replay_one, tasks))
    else:
        rows = []
        for t in tasks:
            print(f"  as of {t['as_of']} ...", flush=True)
            rows.append(replay_one(t))
    df = pd.DataFrame(rows)
    RESULTS.mkdir(exist_ok=True)
    sym = coin if coin.endswith(a.quote.upper()) else coin + a.quote.upper()
    out = RESULTS / f"verdict_backtest_{sym}_{a.tf}.csv"
    df.to_csv(out, index=False)
    if "outcome" in df:
        print("\n" + summarize(df))
    err = df["error"].notna().sum() if "error" in df else 0
    print(f"\nSaved {len(df)} rows ({err} errors) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
