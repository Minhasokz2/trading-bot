"""Download official Binance Spot kline history from data.binance.vision
(the bulk files documented in github.com/binance/binance-public-data).

Raw zips are kept untouched in data/raw/ (with SHA-256 checksum verification);
a cleaned, de-duplicated parquet per symbol/timeframe goes to data/clean/.
Keeping raw and clean separate means every research result can be reproduced.

Usage:
    python download_history.py SOL                  # SOLUSDT 4h, last 24 months
    python download_history.py SOL ETH BTC --tf 1h 4h 1d --months 36
    python download_history.py SOL --aggtrades 7    # also last 7 days of aggregate trades
"""
from __future__ import annotations

import argparse
import hashlib
import io
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/spot"
ROOT = Path(__file__).resolve().parent / "data"
RAW, CLEAN = ROOT / "raw", ROOT / "clean"
KCOLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
         "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
ACOLS = ["agg_id", "price", "qty", "first_id", "last_id", "time", "is_buyer_maker", "best_match"]
S = requests.Session()


def _fetch(url: str, dest: Path) -> Path | None:
    """Download url -> dest once, verifying the published SHA-256 checksum."""
    if dest.exists():
        return dest
    r = S.get(url, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    c = S.get(url + ".CHECKSUM", timeout=30)
    if c.status_code == 200:
        expected = c.text.split()[0].strip()
        got = hashlib.sha256(r.content).hexdigest()
        if expected != got:
            raise RuntimeError(f"Checksum mismatch for {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return dest


def _read_zip(path: Path, cols: list[str]) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    first = raw.split(b"\n", 1)[0]
    header = 0 if first[:1].isalpha() else None       # some files carry a header row
    df = pd.read_csv(io.BytesIO(raw), header=header)
    df.columns = cols[:len(df.columns)]
    return df


def _to_ms(s: pd.Series) -> pd.Series:
    # Spot files switched from milliseconds to microseconds on 2025-01-01.
    s = s.astype("int64")
    return s.where(s < 10**14, s // 1000)


def klines(symbol: str, tf: str, months: int) -> Path:
    today = date.today()
    files = []
    m = date(today.year, today.month, 1)
    for _ in range(months):
        m = (m - timedelta(days=1)).replace(day=1)
        name = f"{symbol}-{tf}-{m:%Y-%m}.zip"
        files.append((f"{BASE}/monthly/klines/{symbol}/{tf}/{name}", RAW / "klines" / symbol / tf / name))
    d = date(today.year, today.month, 1)
    while d < today:                                   # current month: daily files
        name = f"{symbol}-{tf}-{d:%Y-%m-%d}.zip"
        files.append((f"{BASE}/daily/klines/{symbol}/{tf}/{name}", RAW / "klines" / symbol / tf / name))
        d += timedelta(days=1)

    frames = []
    for url, dest in sorted(files, key=lambda x: x[1].name):
        p = _fetch(url, dest)
        if p:
            frames.append(_read_zip(p, KCOLS))
    if not frames:
        raise SystemExit(f"No files found for {symbol} {tf} — is it listed on Binance Spot?")
    df = pd.concat(frames, ignore_index=True)
    df["open_time"], df["close_time"] = _to_ms(df["open_time"]), _to_ms(df["close_time"])
    df = df.drop(columns=["ignore"]).drop_duplicates("open_time").sort_values("open_time")
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    out = CLEAN / f"{symbol}_{tf}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.set_index("time").to_parquet(out)
    print(f"  {symbol} {tf}: {len(df):,} candles {df['time'].iloc[0]:%Y-%m-%d} -> "
          f"{df['time'].iloc[-1]:%Y-%m-%d}  ->  {out.relative_to(ROOT.parent)}")
    return out


def aggtrades(symbol: str, days: int) -> Path | None:
    frames = []
    for k in range(days, 0, -1):
        d = date.today() - timedelta(days=k)
        name = f"{symbol}-aggTrades-{d:%Y-%m-%d}.zip"
        p = _fetch(f"{BASE}/daily/aggTrades/{symbol}/{name}", RAW / "aggTrades" / symbol / name)
        if p:
            frames.append(_read_zip(p, ACOLS))
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(_to_ms(df["time"]), unit="ms", utc=True)
    df["is_buyer_maker"] = df["is_buyer_maker"].astype(str).str.lower().eq("true")
    df["quote"] = df["price"].astype(float) * df["qty"].astype(float)
    # 15-minute taker-flow summary: sell-initiated when buyer is maker
    df["taker_buy_quote"] = df["quote"].where(~df["is_buyer_maker"], 0.0)
    g = df.set_index("time").resample("15min").agg(
        {"quote": "sum", "taker_buy_quote": "sum", "agg_id": "count"}).rename(columns={"agg_id": "agg_trades"})
    g["taker_imbalance"] = 2 * g["taker_buy_quote"] / g["quote"].where(g["quote"] > 0) - 1
    out = CLEAN / f"{symbol}_aggflow_15m.parquet"
    g.to_parquet(out)
    print(f"  {symbol} aggTrades: {len(df):,} trades over {days} days  ->  {out.relative_to(ROOT.parent)}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("coins", nargs="+")
    ap.add_argument("--quote", default="USDT")
    ap.add_argument("--tf", nargs="+", default=["4h"])
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--aggtrades", type=int, default=0, help="days of aggregate trades (0 = skip)")
    a = ap.parse_args()
    for c in a.coins:
        sym = c.upper() if c.upper().endswith(a.quote) else c.upper() + a.quote
        print(f"{sym}:")
        for tf in a.tf:
            klines(sym, tf, a.months)
        if a.aggtrades:
            aggtrades(sym, a.aggtrades)


if __name__ == "__main__":
    main()
