"""Fast parameter sweep of the trend strategy with VectorBT (polakowo/vectorbt).

Purpose: reject weak ideas quickly BEFORE the slower, realistic Freqtrade backtest.
Parameters are chosen on the in-sample period only (first 70%) and then
scored once on the unseen out-of-sample period (last 30%).

Signals on bar close, fills at the next bar's open, 0.1% fee + 0.05% slippage per side.

Usage (run download_history.py first):
    python sweep_vectorbt.py SOL
    python sweep_vectorbt.py SOL --tf 1h --top 15
"""
from __future__ import annotations

import argparse
import itertools
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta_classic as ta
import vectorbt as vbt

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent
FEE, SLIP = 0.001, 0.0005
GRID = {
    "ema_fast": [20, 30, 50],
    "ema_slow": [100, 150, 200],
    "rsi_min": [45, 50, 55],
    "adx_min": [15, 20, 25],
    "atr_stop": [1.5, 2.0, 3.0],
}
FREQ = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}


def load(symbol: str, tf: str) -> pd.DataFrame:
    p = ROOT / "data" / "clean" / f"{symbol}_{tf}.parquet"
    if not p.exists():
        raise SystemExit(f"{p.name} not found. Run:  python download_history.py "
                         f"{symbol.replace('USDT', '')} --tf {tf}")
    return pd.read_parquet(p)


def run(df: pd.DataFrame, fast, slow, rsi_min, adx_min, atr_stop, freq, start: int = 0):
    """start: bars used only to warm up indicators; trading begins after them."""
    c, o = df["close"], df["open"]
    ef, es = ta.ema(c, length=fast), ta.ema(c, length=slow)
    rsi = ta.rsi(c, length=14)
    adx = ta.adx(df["high"], df["low"], c, length=14)["ADX_14"]
    atr = ta.atr(df["high"], df["low"], c, length=14)
    entry = (c > ef) & (ef > es) & (rsi > rsi_min) & (adx > adx_min)
    exit_ = c < ef
    # decided on close of bar t -> executed at open of bar t+1
    entries = entry.shift(1, fill_value=False).astype(bool)
    exits = exit_.shift(1, fill_value=False).astype(bool)
    sl = (atr_stop * atr / c).shift(1).bfill()
    k = slice(start, None)
    return vbt.Portfolio.from_signals(c[k], entries[k], exits[k], price=o[k], fees=FEE,
                                      slippage=SLIP, sl_stop=sl[k], freq=freq, init_cash=10_000)


def summary(pf) -> dict:
    t = pf.trades
    n = int(t.count())
    return {"trades": n,
            "return_pct": round(float(pf.total_return()) * 100, 2),
            "sharpe": round(float(pf.sharpe_ratio()), 2) if n else 0.0,
            "sortino": round(float(pf.sortino_ratio()), 2) if n else 0.0,
            "max_dd_pct": round(float(pf.max_drawdown()) * 100, 2),
            "win_rate": round(float(t.win_rate()), 3) if n else 0.0,
            "profit_factor": round(float(t.profit_factor()), 2) if n else 0.0,
            "exposure_pct": round(float((pf.position_mask()).mean()) * 100, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("coin")
    ap.add_argument("--quote", default="USDT")
    ap.add_argument("--tf", default="4h")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-trades", type=int, default=10)
    a = ap.parse_args()
    sym = a.coin.upper() if a.coin.upper().endswith(a.quote) else a.coin.upper() + a.quote
    df = load(sym, a.tf)
    if len(df) < 1000:
        raise SystemExit(f"Only {len(df)} candles — download more history (--months).")
    split = int(len(df) * 0.7)
    warm = 250                                          # indicator warm-up carried into OOS
    ins, oos = df.iloc[:split], df.iloc[split - warm:]
    freq = FREQ.get(a.tf, a.tf)

    combos = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())
              if v[0] < v[1]]
    print(f"{sym} {a.tf}: {len(df):,} candles; testing {len(combos)} parameter sets "
          f"in-sample ({ins.index[0]:%Y-%m-%d} -> {ins.index[-1]:%Y-%m-%d}) ...")
    rows = []
    for p in combos:
        s = summary(run(ins, p["ema_fast"], p["ema_slow"], p["rsi_min"], p["adx_min"], p["atr_stop"], freq))
        rows.append({**p, **{f"is_{k}": v for k, v in s.items()}})
    res = pd.DataFrame(rows)
    res = res[res["is_trades"] >= a.min_trades].sort_values("is_sharpe", ascending=False)
    if res.empty:
        raise SystemExit("No parameter set produced enough trades in-sample.")

    top = res.head(a.top).copy()
    oos_rows = []
    for _, p in top.iterrows():
        pf = run(oos, int(p.ema_fast), int(p.ema_slow), p.rsi_min, p.adx_min, p.atr_stop, freq, warm)
        oos_rows.append({f"oos_{k}": v for k, v in summary(pf).items()})
    top = pd.concat([top.reset_index(drop=True), pd.DataFrame(oos_rows)], axis=1)
    bh_oos = float(oos["close"].iloc[-1] / oos["close"].iloc[warm] - 1) * 100

    out_dir = ROOT / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"sweep_{sym}_{a.tf}.csv"
    top.to_csv(out, index=False)
    cols = ["ema_fast", "ema_slow", "rsi_min", "adx_min", "atr_stop", "is_trades", "is_sharpe",
            "is_return_pct", "oos_trades", "oos_sharpe", "oos_return_pct", "oos_max_dd_pct"]
    print(top[cols].to_string(index=False))
    held = (top["oos_sharpe"] > 0).mean()
    print(f"\nOut-of-sample buy & hold: {bh_oos:+.1f}%")
    print(f"{held:.0%} of the top {len(top)} in-sample sets stayed profitable (Sharpe>0) out-of-sample.")
    print(f"Tested {len(combos)} combinations: the best in-sample result is partly luck. "
          "Only trust parameters that hold up out-of-sample AND in the Freqtrade backtest.")
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
