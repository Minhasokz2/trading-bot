"""Read-only market data clients.

* BinanceClient  — Binance Spot public market data via the official SDK
                   (binance/binance-connector-python -> package `binance-sdk-spot`), using the
                   dedicated public market-data host (data-api.binance.vision). Optional local kline
                   cache (parquet) and point-in-time mode (`as_of`) for replays.
* FuturesClient  — USD-M futures public data (funding, mark price, open interest) via the official
                   `binance-sdk-derivatives-trading-usds-futures` SDK. Read-only.
* OfflineClient  — the same interface served from local parquet files (research/data/clean, the
                   kline cache, or synthetic data). Used by --offline, --demo, tests and CI.

No API key is created, stored or needed anywhere. No order endpoint is ever called.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
FRAME_COLS = KLINE_COLS[:-1]
FLOAT_COLS = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]
INT_COLS = ["open_time", "close_time", "trades"]
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
         "1d": 86_400_000, "1w": 604_800_000}
MARKET_DATA_URL = "https://data-api.binance.vision"   # Binance's public market-data host (no key, read-only)
MAX_REFILL = 3000                                      # a cache this many candles behind is rebuilt, not extended
LEVERAGED = ("UP", "DOWN", "BEAR", "BULL")
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "EUR", "EURI", "AEUR", "PYUSD",
                "USDE", "XUSD", "USDD", "GUSD", "PAXG", "XAUT"}


class BinanceError(RuntimeError):
    pass


def _plain(obj):
    """SDK responses are pydantic models; turn them into plain dict/list."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, list):
        return [_plain(x) for x in obj]
    return obj


def _records(obj) -> list[dict]:
    """A list of plain dicts out of whatever an SDK "list" endpoint returned (a list of models, a RootModel
    dump wrapped in {"root": [...]}, or a single record)."""
    obj = _plain(obj)
    if isinstance(obj, dict):
        if len(obj) == 1 and next(iter(obj)) in ("root", "actual_instance", "data"):
            return _records(next(iter(obj.values())))
        return [obj] if "symbol" in obj else []
    if isinstance(obj, list):
        return [x for x in (_plain(y) for y in obj) if isinstance(x, dict) and "symbol" in x]
    return []


def empty_frame() -> pd.DataFrame:
    """A typed, empty kline frame (float prices and volumes, int64 times, UTC DatetimeIndex 'time')."""
    cols = {c: pd.Series(dtype="float64") for c in FLOAT_COLS}
    cols.update({c: pd.Series(dtype="int64") for c in INT_COLS})
    return pd.DataFrame(cols, index=pd.DatetimeIndex([], tz="UTC", name="time"))[FRAME_COLS]


def normalise_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Force the kline dtypes and a sorted, unique UTC index whatever produced the frame.

    Every frame that leaves the data layer goes through here. The reason it exists: pandas 3 no longer
    ignores an empty frame in `concat`, so `concat([pd.DataFrame(columns=...), candles])` turns every
    column into `object` — `np.log(df["close"])` then fails with "loop of ufunc does not support argument
    0 of type float", which is exactly how the first live audit on a fresh cache used to die."""
    if df is None or len(df) == 0:
        return empty_frame()
    cols = [c for c in FRAME_COLS if c in df.columns]
    out = df[cols].copy()
    if "open_time" not in out.columns:
        if not isinstance(out.index, pd.DatetimeIndex):
            raise BinanceError("kline frame has neither open_time nor a datetime index")
        out["open_time"] = epoch_ms(out.index.tz_localize("UTC") if out.index.tz is None else out.index)
    for c in FLOAT_COLS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    for c in INT_COLS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=[c for c in ("open", "high", "low", "close", "open_time") if c in out.columns])
    for c in INT_COLS:
        if c in out.columns:
            out[c] = out[c].astype("int64")
    if isinstance(out.index, pd.DatetimeIndex) and out.index.tz is not None:
        out.index = out.index.tz_convert("UTC")
    else:
        out.index = pd.to_datetime(out["open_time"], unit="ms", utc=True)
    out.index.name = "time"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.attrs = {}
    return out


def merge_frames(*frames: pd.DataFrame) -> pd.DataFrame:
    """Concatenate kline frames (later ones win on duplicate candles) without ever letting an empty frame
    into `concat` (see normalise_frame)."""
    parts = [f for f in frames if f is not None and len(f)]
    if not parts:
        return empty_frame()
    return normalise_frame(pd.concat(parts) if len(parts) > 1 else parts[0])


def to_frame(rows: list) -> pd.DataFrame:
    if not rows:
        return empty_frame()
    df = pd.DataFrame(rows, columns=KLINE_COLS).drop(columns=["ignore"])
    return normalise_frame(df)


def epoch_ms(index: pd.DatetimeIndex) -> np.ndarray:
    """Epoch milliseconds for any datetime resolution (pandas 3 defaults to microseconds)."""
    return index.as_unit("ms").asi8.astype("int64")


def drop_forming(df: pd.DataFrame, now_ms: int | None = None) -> pd.DataFrame:
    """Drop Binance's still-forming candle: only candles whose close_time is in the past are closed."""
    if df.empty:
        return df
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return df[df["close_time"].astype("int64") < now_ms]


def _cut(df: pd.DataFrame, as_of_ms: int | None) -> pd.DataFrame:
    """Keep only candles that had CLOSED at as_of (point-in-time view)."""
    if df.empty or as_of_ms is None:
        return df
    return df[df["close_time"].astype("int64") <= as_of_ms]


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Aggregate a finer kline frame to a coarser Binance timeframe (weekly candles open Monday)."""
    rule = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D", "1w": "W-MON"}[tf]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
           "quote_volume": "sum", "trades": "sum", "taker_buy_base": "sum", "taker_buy_quote": "sum"}
    o = df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["open"])
    o["open_time"] = o.index.as_unit("ms").asi8.astype("int64")
    o["close_time"] = o["open_time"] + TF_MS[tf] - 1
    o["trades"] = o["trades"].astype(int)
    o.index.name = "time"
    o = o[FRAME_COLS]
    o.attrs = {}
    return o


# ------------------------------------------------------ point-in-time helpers
def synthetic_ticker(symbol: str, h1: pd.DataFrame) -> dict:
    """A 24h ticker rebuilt from the last 24 hourly candles (used in --as-of and offline modes)."""
    last = h1.tail(24)
    if last.empty:
        raise BinanceError(f"No candles to build a ticker for {symbol}")
    first_open = float(last["open"].iloc[0])
    close = float(last["close"].iloc[-1])
    return {"symbol": symbol, "lastPrice": str(close), "quoteVolume": str(float(last["quote_volume"].sum())),
            "volume": str(float(last["volume"].sum())), "highPrice": str(float(last["high"].max())),
            "lowPrice": str(float(last["low"].min())), "count": int(last["trades"].sum()),
            "priceChangePercent": str(round((close / first_open - 1) * 100, 3)) if first_open else "0",
            "synthetic": True}


def synthetic_book(price: float, quote_volume_24h: float, spread_bps: float = 5.0,
                   depth_ratio: float = 0.02, levels: int = 25) -> dict:
    """A plausible order book: ±1% depth = depth_ratio x 24h quote volume, spread in bps, linear levels."""
    half = price * spread_bps / 2e4
    side_depth = max(quote_volume_24h * depth_ratio / 2, 1.0)
    per_level_usd = side_depth / levels
    bids, asks = [], []
    for k in range(levels):
        pb = price - half - price * 0.01 * k / levels
        pa = price + half + price * 0.01 * k / levels
        bids.append([str(pb), str(per_level_usd / pb)])
        asks.append([str(pa), str(per_level_usd / pa)])
    return {"bids": bids, "asks": asks, "synthetic": True}


def default_tick(price: float) -> float:
    if price <= 0:
        return 0.0
    return 10.0 ** (math.floor(math.log10(price)) - 4)


def is_tradable_symbol(symbol: str, quote: str) -> bool:
    if not symbol.endswith(quote) or len(symbol) <= len(quote):
        return False
    base = symbol[:-len(quote)]
    if base in STABLE_BASES:
        return False
    return not any(base.endswith(x) for x in LEVERAGED)


# ------------------------------------------------------------------ online
class BinanceClient:
    """Read-only Spot market data. `cache_dir` keeps every downloaded kline in parquet so repeated
    audits are fast and replays (--as-of) need no re-download; `as_of` (epoch ms or ISO string)
    turns the client into a point-in-time view: candles after as_of are invisible and the 24h
    ticker / order book are rebuilt from candles."""

    def __init__(self, timeout_ms: int = 15000, retries: int = 3, cache_dir: str | Path | None = None,
                 as_of=None, api=None):
        """`api` lets tests inject a stand-in for the SDK's REST API (same method names and response shapes)."""
        if api is not None:
            self.api, self._intervals = api, {k: k for k in TF_MS}
        else:
            from binance_common.constants import SPOT_REST_API_MARKET_URL
            from binance_sdk_spot.spot import Spot, ConfigurationRestAPI
            from binance_sdk_spot.rest_api.models import KlinesIntervalEnum
            cfg = ConfigurationRestAPI(base_path=SPOT_REST_API_MARKET_URL, timeout=timeout_ms, retries=retries)
            self.api = Spot(config_rest_api=cfg).rest_api
            self._intervals = {e.value: e for e in KlinesIntervalEnum}
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.as_of_ms = to_ms(as_of)
        self._symbols = None
        self.http_get = requests.get                     # plain-HTTP fallback for list endpoints (tests swap it)

    # ----------------------------------------------------------------- core
    def _call(self, fn, **kw):
        last = None
        for attempt in range(3):
            try:
                return _plain(fn(**kw).data())
            except Exception as e:  # SDK raises typed errors; keep message
                msg = str(e)
                if "Invalid symbol" in msg or "-1121" in msg:
                    raise BinanceError("Invalid symbol") from e
                last = msg
                time.sleep(1 + attempt * 2)
        raise BinanceError(f"Could not reach Binance: {last}")

    def symbol_info(self, symbol: str) -> dict | None:
        try:
            data = self._call(self.api.exchange_info, symbol=symbol)
        except BinanceError as e:
            if "Invalid symbol" in str(e):
                return None
            raise
        syms = data.get("symbols") or []
        return syms[0] if syms else None

    def all_symbols(self) -> set:
        """Every Spot symbol currently TRADING (one exchangeInfo call, cached on the client)."""
        if self._symbols is None:
            data = self._call(self.api.exchange_info)
            self._symbols = {s["symbol"] for s in data.get("symbols", []) if s.get("status") == "TRADING"}
        return self._symbols

    def ticker_24h(self, symbol: str) -> dict:
        if self.as_of_ms is not None:
            return synthetic_ticker(symbol, self.klines(symbol, "1h", 30))
        return self._call(self.api.ticker24hr, symbol=symbol)

    def ticker_24h_all(self) -> list[dict]:
        """24h tickers for every symbol (one call) — used by the market scanner. If the SDK's answer is not
        the list of records the scanner needs, the public REST endpoint is read directly."""
        if self.as_of_ms is not None:
            raise BinanceError("--scan needs live tickers; it cannot run point-in-time")
        try:
            rows = _records(self._call(self.api.ticker24hr))
        except BinanceError:
            rows = []
        if len(rows) < 2:
            rows = _records(self._http_json("/api/v3/ticker/24hr"))
        if len(rows) < 2:
            raise BinanceError("Could not read the 24h tickers from Binance (unexpected response)")
        return rows

    def _http_json(self, path: str):
        try:
            r = self.http_get(MARKET_DATA_URL + path, timeout=20, headers={"User-Agent": "coin-audit-bot"})
            return r.json() if getattr(r, "status_code", 0) == 200 else None
        except (requests.RequestException, ValueError) as e:
            raise BinanceError(f"Could not reach Binance: {e}") from e

    def order_book(self, symbol: str, limit: int = 500) -> dict:
        if self.as_of_ms is not None:
            t = self.ticker_24h(symbol)
            return synthetic_book(float(t["lastPrice"]), float(t["quoteVolume"]))
        return self._call(self.api.depth, symbol=symbol, limit=limit)

    # --------------------------------------------------------------- klines
    def _klines_api(self, symbol: str, interval: str, bars: int, start_ms: int | None = None,
                    end_ms: int | None = None) -> pd.DataFrame:
        iv = self._intervals[interval]
        rows: list = []
        if start_ms is not None:
            cursor = start_ms
            while len(rows) < bars:
                kw = {"symbol": symbol, "interval": iv, "start_time": cursor, "limit": min(1000, bars - len(rows))}
                if end_ms is not None:
                    kw["end_time"] = end_ms
                batch = self._call(self.api.klines, **kw)
                if not batch:
                    break
                rows.extend(batch)
                cursor = int(batch[-1][0]) + 1
                if len(batch) < 1000:
                    break
        else:
            end = end_ms
            while len(rows) < bars:
                lim = min(1000, bars - len(rows))
                kw = {"symbol": symbol, "interval": iv, "limit": lim}
                if end is not None:
                    kw["end_time"] = end
                batch = self._call(self.api.klines, **kw)
                if not batch:
                    break
                rows = batch + rows
                end = int(batch[0][0]) - 1
                if len(batch) < lim:
                    break
        return drop_forming(to_frame(rows))

    def klines(self, symbol: str, interval: str, bars: int = 1000, start_ms: int | None = None) -> pd.DataFrame:
        # Binance's endTime bound includes the candle that was still forming at as_of; _cut removes it, so
        # one extra candle is requested in point-in-time mode to still return `bars` closed ones.
        extra = 1 if self.as_of_ms is not None else 0
        if start_ms is not None:
            return _cut(self._klines_api(symbol, interval, bars + extra, start_ms=start_ms, end_ms=self.as_of_ms),
                        self.as_of_ms).head(bars)
        if self.cache_dir is None:
            return _cut(self._klines_api(symbol, interval, bars + extra, end_ms=self.as_of_ms), self.as_of_ms).tail(bars)
        return self._klines_cached(symbol, interval, bars)

    def _klines_cached(self, symbol: str, interval: str, bars: int) -> pd.DataFrame:
        """The newest `bars` closed candles, served from `<cache_dir>/<SYMBOL>_<tf>.parquet` plus whatever
        Binance has since. The file always holds a contiguous, typed, closed-candles-only series: a cache
        that fell too far behind is rebuilt rather than extended with a hole in it."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{symbol}_{interval}.parquet"
        cached = normalise_frame(pd.read_parquet(path)) if path.exists() else empty_frame()
        step = TF_MS.get(interval, TF_MS["1d"])
        if len(cached) and (self.as_of_ms is None or int(cached["close_time"].iloc[-1]) < self.as_of_ms):
            horizon = self.as_of_ms if self.as_of_ms is not None else int(time.time() * 1000)
            last_open = int(cached["open_time"].iloc[-1])
            missing = max(0, (horizon - last_open) // step)   # candles opened since the newest cached one
            if missing > bars + MAX_REFILL:
                cached = empty_frame()
            elif missing:
                newer = self._klines_api(symbol, interval, missing + 2, start_ms=last_open + 1, end_ms=self.as_of_ms)
                cached = merge_frames(cached, newer)
        view = _cut(cached, self.as_of_ms)
        if len(view) < bars:                                # backfill older history
            end = int(view["open_time"].iloc[0]) - 1 if len(view) else self.as_of_ms
            extra = 1 if (self.as_of_ms is not None and not len(view)) else 0   # see klines()
            older = self._klines_api(symbol, interval, bars - len(view) + extra, end_ms=end)
            cached = merge_frames(older, cached)
        if len(cached):
            cached.to_parquet(path)
        return _cut(cached, self.as_of_ms).tail(bars)


def to_ms(as_of) -> int | None:
    if as_of is None or as_of == "":
        return None
    if isinstance(as_of, (int, np.integer)):
        return int(as_of)
    if isinstance(as_of, float):
        return int(as_of)
    ts = pd.Timestamp(as_of)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(ts.value // 1_000_000)


# ------------------------------------------------------------ USD-M futures
class FuturesClient:
    """Read-only USD-M futures public data (funding, mark/index price, open interest) via the official
    `binance-sdk-derivatives-trading-usds-futures` SDK. Nothing here can trade."""

    def __init__(self, timeout_ms: int = 15000):
        from binance_sdk_derivatives_trading_usds_futures.derivatives_trading_usds_futures import (
            DerivativesTradingUsdsFutures, ConfigurationRestAPI as FCfg,
            DERIVATIVES_TRADING_USDS_FUTURES_REST_API_PROD_URL as URL)
        self.api = DerivativesTradingUsdsFutures(
            config_rest_api=FCfg(base_path=URL, timeout=timeout_ms, retries=2)).rest_api

    def _call(self, fn, **kw):
        try:
            return _plain(fn(**kw).data())
        except Exception as e:
            raise BinanceError(str(e)) from e

    def funding_history(self, symbol: str, limit: int = 90) -> list[dict]:
        return self._call(self.api.get_funding_rate_history, symbol=symbol, limit=limit)

    def mark_price(self, symbol: str) -> dict:
        return self._call(self.api.mark_price, symbol=symbol)

    def funding_series(self, symbol: str, start_ms: int, max_records: int = 6000) -> pd.DataFrame:
        """All funding settlements since start_ms (8h each), paginated 1000 at a time."""
        rows, cur = [], start_ms
        while len(rows) < max_records:
            batch = self._call(self.api.get_funding_rate_history, symbol=symbol, start_time=cur, limit=1000)
            if not batch:
                break
            rows += batch
            cur = int(batch[-1]["fundingTime"]) + 1
            if len(batch) < 1000:
                break
        return funding_frame(rows)


def funding_frame(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["rate"])
    d = pd.DataFrame(rows)
    d["time"] = pd.to_datetime(d["fundingTime"].astype("int64"), unit="ms", utc=True)
    d["rate"] = d["fundingRate"].astype(float)
    return d.drop_duplicates("time").set_index("time").sort_index()[["rate"]]


# ----------------------------------------------------------------- offline
class OfflineClient:
    """BinanceClient's interface served from local parquet files: `<dir>/<SYMBOL>_<tf>.parquet`
    (the layout written by research/download_history.py and by the kline cache). A missing timeframe
    is resampled from the finest one available. `as_of` gives a point-in-time view."""

    TFS = ["15m", "1h", "4h", "1d", "1w"]

    def __init__(self, data_dir: str | Path, as_of=None, quote: str = "USDT", spread_bps: float = 5.0,
                 depth_ratio: float = 0.02):
        self.dir = Path(data_dir)
        self.as_of_ms = to_ms(as_of)
        self.quote, self.spread_bps, self.depth_ratio = quote, spread_bps, depth_ratio
        self._frames: dict = {}

    # ---------------------------------------------------------- inventory
    def _files(self, symbol: str) -> dict:
        out = {}
        for tf in self.TFS:
            p = self.dir / f"{symbol}_{tf}.parquet"
            if p.exists():
                out[tf] = p
        return out

    def all_symbols(self) -> set:
        return {p.name.rsplit("_", 1)[0] for p in self.dir.glob("*_*.parquet")
                if p.name.rsplit("_", 1)[1].replace(".parquet", "") in self.TFS}

    def _full(self, symbol: str, tf: str) -> pd.DataFrame:
        key = (symbol, tf)
        if key in self._frames:
            return self._frames[key]
        files = self._files(symbol)
        if not files:
            raise BinanceError(f"{symbol}: no local data in {self.dir}")
        if tf in files:
            df = pd.read_parquet(files[tf])
        else:
            finer = [t for t in self.TFS if t in files and TF_MS[t] < TF_MS[tf]]
            if not finer:
                raise BinanceError(f"{symbol}: no {tf} data and nothing finer to resample from")
            df = resample(self._full(symbol, finer[0]), tf)
        if "open_time" not in df.columns:
            idx = pd.DatetimeIndex(df.index)
            df = df.assign(open_time=epoch_ms(idx.tz_localize("UTC") if idx.tz is None else idx))
        if "close_time" not in df.columns:
            df = df.assign(close_time=df["open_time"].astype("int64") + TF_MS[tf] - 1)
        self._frames[key] = normalise_frame(df)                 # typed columns, sorted unique index, no attrs
        return self._frames[key]

    # --------------------------------------------------------- interface
    def symbol_info(self, symbol: str) -> dict | None:
        if not self._files(symbol):
            return None
        price = float(self._full(symbol, "1d")["close"].iloc[-1])
        base = symbol[:-len(self.quote)] if symbol.endswith(self.quote) else symbol
        return {"symbol": symbol, "status": "TRADING", "baseAsset": base, "quoteAsset": self.quote,
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": str(default_tick(price))}]}

    def klines(self, symbol: str, interval: str, bars: int = 1000, start_ms: int | None = None) -> pd.DataFrame:
        df = _cut(self._full(symbol, interval), self.as_of_ms)
        if start_ms is not None:
            return df[df["open_time"].astype("int64") >= start_ms].head(bars)
        return df.tail(bars)

    def ticker_24h(self, symbol: str) -> dict:
        return synthetic_ticker(symbol, self.klines(symbol, "1h", 30))

    def ticker_24h_all(self) -> list[dict]:
        out = []
        for s in sorted(self.all_symbols()):
            try:
                out.append(self.ticker_24h(s))
            except BinanceError:
                continue
        return out

    def order_book(self, symbol: str, limit: int = 500) -> dict:
        t = self.ticker_24h(symbol)
        return synthetic_book(float(t["lastPrice"]), float(t["quoteVolume"]), self.spread_bps, self.depth_ratio)


class OfflineFuturesClient:
    """Deterministic funding / open-interest stand-in for offline runs. Reads `<SYMBOL>_funding.parquet`
    (columns fundingTime, fundingRate) when present, otherwise synthesises a plausible series."""

    def __init__(self, data_dir: str | Path, as_of=None, seed: int = 0):
        self.dir, self.as_of_ms, self.seed = Path(data_dir), to_ms(as_of), seed
        self.api = _OfflineFuturesApi(self)

    def _spot(self, symbol: str) -> pd.DataFrame | None:
        for tf in ("1d", "4h", "1h", "15m"):
            p = self.dir / f"{symbol}_{tf}.parquet"
            if p.exists():
                return _cut(pd.read_parquet(p), self.as_of_ms)
        return None

    def _series(self, symbol: str) -> pd.DataFrame:
        p = self.dir / f"{symbol}_funding.parquet"
        if p.exists():
            d = pd.read_parquet(p)
            rows = d.to_dict("records")
        else:
            spot = self._spot(symbol)
            start = int(spot["open_time"].iloc[0]) if spot is not None and len(spot) else 1_672_531_200_000
            end = self.as_of_ms or int(time.time() * 1000)
            times = np.arange(start, end, 8 * 3_600_000)
            rng = np.random.default_rng(self.seed + sum(map(ord, symbol)))
            rate = 0.0001 + 0.00015 * np.sin(np.arange(len(times)) / 40) + rng.normal(0, 0.00005, len(times))
            rows = [{"symbol": symbol, "fundingTime": int(t), "fundingRate": float(r)} for t, r in zip(times, rate)]
        d = funding_frame(rows)
        return _cut(d.assign(close_time=epoch_ms(d.index)), self.as_of_ms)[["rate"]]

    def _call(self, fn, **kw):
        return fn(**kw)

    def funding_history(self, symbol: str, limit: int = 90) -> list[dict]:
        s = self._series(symbol).tail(limit)
        return [{"symbol": symbol, "fundingTime": int(t.value // 1_000_000), "fundingRate": str(r)}
                for t, r in s["rate"].items()]

    def mark_price(self, symbol: str) -> dict:
        spot = self._spot(symbol)
        px = float(spot["close"].iloc[-1]) if spot is not None and len(spot) else 100.0
        s = self._series(symbol)
        return {"symbol": symbol, "markPrice": str(px * 1.0002), "indexPrice": str(px),
                "lastFundingRate": str(float(s["rate"].iloc[-1]) if len(s) else 0.0)}

    def funding_series(self, symbol: str, start_ms: int, max_records: int = 6000) -> pd.DataFrame:
        s = self._series(symbol)
        return s[epoch_ms(s.index) >= start_ms].head(max_records)


class _OfflineFuturesApi:
    """The subset of the futures REST API that market.derivatives() calls, with plausible numbers."""

    def __init__(self, owner: OfflineFuturesClient):
        self.o = owner

    def _rng(self, symbol):
        return np.random.default_rng(self.o.seed + 7 + sum(map(ord, symbol)))

    def open_interest_statistics(self, symbol, period=None, limit=200, **_):
        rng = self._rng(symbol)
        oi = 50e6 * np.exp(np.cumsum(rng.normal(0, 0.01, limit)))
        return [{"symbol": symbol, "sumOpenInterestValue": str(float(v))} for v in oi]

    def long_short_ratio(self, symbol, period=None, limit=24, **_):
        rng = self._rng(symbol)
        return [{"symbol": symbol, "longShortRatio": str(float(1.0 + rng.normal(0, 0.1)))} for _ in range(limit)]

    def top_trader_long_short_ratio_positions(self, symbol, period=None, limit=24, **_):
        rng = self._rng(symbol)
        return [{"symbol": symbol, "longShortRatio": str(float(1.2 + rng.normal(0, 0.1)))} for _ in range(limit)]

    def taker_buy_sell_volume(self, symbol, period=None, limit=24, **_):
        rng = self._rng(symbol)
        return [{"symbol": symbol, "buySellRatio": str(float(1.0 + rng.normal(0, 0.05)))} for _ in range(limit)]
