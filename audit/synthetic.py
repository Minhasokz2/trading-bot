"""Synthetic Binance-like market data for offline use (tests, --demo, CI).

Generates regime-switching OHLCV candles that look like the real kline frames
(same columns as binance_client.to_frame), resamples them to higher timeframes,
and can *plant* textbook chart patterns so the detectors can be tested without
any network access. Nothing here is used for live audits.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
# regime: (drift per year, volatility multiplier)
REGIMES = {"bull": (1.2, 0.9), "bear": (-0.9, 1.1), "chop": (0.0, 0.6), "crash": (-3.0, 2.2)}
_TRANSITION = np.array([  # rows: from bull, bear, chop, crash
    [0.9970, 0.0010, 0.0017, 0.0003],
    [0.0012, 0.9965, 0.0018, 0.0005],
    [0.0020, 0.0015, 0.9963, 0.0002],
    [0.0040, 0.0080, 0.0030, 0.9850],
])
_STATES = list(REGIMES)
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
              "taker_buy_base", "taker_buy_quote"]


def gbm_ohlcv(n: int, tf: str = "15m", seed: int = 0, start: str = "2023-01-01", price: float = 100.0,
              ann_vol: float = 0.8, drift: float = 0.0, regimes: bool = True,
              base_volume: float = 50_000.0) -> pd.DataFrame:
    """Regime-switching geometric random walk with fat tails, realistic wicks, volume and taker flow.
    Returns the same frame layout as binance_client.to_frame (UTC index 'time', int64 open_time)."""
    rng = np.random.default_rng(seed)
    bpy = 525_600 / TF_MINUTES[tf]
    if regimes:
        st = np.zeros(n, int)
        u = rng.random(n)
        cum = _TRANSITION.cumsum(axis=1)
        for i in range(1, n):
            st[i] = int(np.searchsorted(cum[st[i - 1]], u[i]))
        mu = np.array([REGIMES[_STATES[s]][0] for s in st]) / bpy
        vm = np.array([REGIMES[_STATES[s]][1] for s in st])
    else:
        st = np.zeros(n, int)
        mu, vm = np.full(n, drift / bpy), np.ones(n)
    sig = ann_vol / np.sqrt(bpy) * vm
    eps = rng.standard_t(4, n) / np.sqrt(2.0)          # unit-variance fat tails
    r = mu + sig * eps
    close = price * np.exp(np.cumsum(r))
    open_ = np.r_[price, close[:-1]] * (1 + rng.normal(0, 0.05, n) * sig)
    body_hi, body_lo = np.maximum(open_, close), np.minimum(open_, close)
    high = body_hi * (1 + np.abs(rng.normal(0, 0.6, n)) * sig)
    low = body_lo * (1 - np.abs(rng.normal(0, 0.6, n)) * sig)
    volume = base_volume * np.exp(rng.normal(0, 0.5, n)) * (1 + 3 * np.abs(r) / np.maximum(sig, 1e-9))
    volume *= (1.0 + 0.5 * (st == 3))
    vwap = (open_ + close) / 2
    quote = volume * vwap
    share = np.clip(0.5 + 0.25 * np.tanh(r / np.maximum(sig, 1e-9)) + rng.normal(0, 0.05, n), 0.05, 0.95)
    taker_base = volume * share
    idx = pd.date_range(start, periods=n, freq=f"{TF_MINUTES[tf]}min", tz="UTC")
    open_ms = idx.as_unit("ms").asi8.astype("int64")
    df = pd.DataFrame({
        "open_time": open_ms, "open": open_, "high": high, "low": low, "close": close, "volume": volume,
        "close_time": open_ms + TF_MINUTES[tf] * 60_000 - 1, "quote_volume": quote,
        "trades": np.maximum(1, (quote / 250.0)).astype(int), "taker_buy_base": taker_base,
        "taker_buy_quote": taker_base * vwap}, index=idx)
    df.index.name = "time"
    return df                      # (no DataFrame.attrs: pandas 3 deep-copies attrs on every operation)


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Aggregate a finer kline frame to a coarser timeframe (Binance-style, UTC-aligned;
    weekly candles open on Monday 00:00 UTC)."""
    rule = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D", "1w": "W-MON"}[tf]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
           "quote_volume": "sum", "trades": "sum", "taker_buy_base": "sum", "taker_buy_quote": "sum"}
    o = df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["open"])
    o["open_time"] = o.index.as_unit("ms").asi8.astype("int64")
    o["close_time"] = o["open_time"] + TF_MINUTES[tf] * 60_000 - 1
    o["trades"] = o["trades"].astype(int)
    o.index.name = "time"
    return o[KLINE_COLS]


# ------------------------------------------------------------------ pattern planting
def _path(anchors: list[tuple[int, float]]) -> np.ndarray:
    """Piecewise-linear close path through (bar, price) anchors; bar 0 is the first painted candle."""
    t = np.array([a[0] for a in anchors], float)
    p = np.array([a[1] for a in anchors], float)
    return np.interp(np.arange(int(t[-1]) + 1), t, p)


def _paint(df: pd.DataFrame, start: int, closes: np.ndarray, rng, vol_mult: np.ndarray, noise: float = 0.0008):
    """Overwrite candles start.. with a smooth path (tiny wicks) so pivots are unambiguous."""
    n = len(closes)
    base_vol = float(df["volume"].iloc[max(0, start - 60):start].mean() or df["volume"].mean())
    prev = float(df["close"].iloc[start - 1]) if start > 0 else closes[0]
    col = {k: df.columns.get_loc(k) for k in ("open", "high", "low", "close", "volume", "quote_volume",
                                              "taker_buy_base", "taker_buy_quote", "trades")}
    for k in range(n):
        i = start + k
        c, o = float(closes[k]), prev
        w1, w2 = abs(rng.normal(0, noise)), abs(rng.normal(0, noise))
        hi, lo = max(o, c) * (1 + w1), min(o, c) * (1 - w2)
        v = base_vol * (0.8 + 0.4 * rng.random()) * vol_mult[k]
        vw, share = (o + c) / 2, (0.62 if c > o else 0.42)
        df.iloc[i, col["open"]], df.iloc[i, col["high"]], df.iloc[i, col["low"]], df.iloc[i, col["close"]] = o, hi, lo, c
        df.iloc[i, col["volume"]], df.iloc[i, col["quote_volume"]] = v, v * vw
        df.iloc[i, col["taker_buy_base"]], df.iloc[i, col["taker_buy_quote"]] = v * share, v * vw * share
        df.iloc[i, col["trades"]] = int(max(1, v * vw / 250))
        prev = c


BULLISH = ["double_bottom", "triple_bottom", "inverse_head_shoulders", "bull_flag", "high_tight_flag",
           "ascending_triangle", "falling_wedge", "rounding_bottom", "cup_and_handle", "v_bottom",
           "range_breakout", "trendline_break"]
BEARISH = ["double_top", "head_shoulders", "descending_triangle"]


def plant(df: pd.DataFrame, kind: str, at: int, scale: float, seed: int = 0, with_volume: bool = True) -> dict:
    """Paint a textbook pattern ending with its breakout candle. `scale` is the typical candle range
    (an ATR-like unit) of the frame BEFORE planting; every geometry below is expressed in that unit.
    Returns {"breakout": index of the breakout candle, "level": the level it closes beyond, ...}.
    with_volume=False paints the same shape with an ordinary-volume breakout (must be rejected)."""
    rng = np.random.default_rng(seed)
    p0 = float(df["close"].iloc[at - 1])
    a = scale
    vol = None
    if kind in ("double_bottom", "triple_bottom"):
        low = p0 - 7 * a
        neck = low + 3.5 * a
        anc = [(0, p0), (22, low), (31, neck), (40, low + 0.02 * a)]
        if kind == "triple_bottom":
            anc += [(49, neck), (58, low + 0.04 * a)]
        t = anc[-1][0]
        anc += [(t + 9, neck - 0.3 * a), (t + 10, neck + 0.6 * a)]
        level = neck
    elif kind == "inverse_head_shoulders":
        ls = p0 - 6 * a
        neck, hd = ls + 3 * a, ls - 2.2 * a
        anc = [(0, p0), (20, ls), (28, neck), (37, hd), (46, neck + 0.2 * a), (54, ls + 0.1 * a),
               (61, neck - 0.3 * a), (62, neck + 1.0 * a)]
        level = neck + 0.38 * a
    elif kind in ("bull_flag", "high_tight_flag"):
        pole = 10 * a if kind == "high_tight_flag" else 6 * a
        top = p0 + pole
        retr = (0.15 if kind == "high_tight_flag" else 0.30) * pole
        anc = [(0, p0), (10, top), (15, top - retr), (18, top - 0.6 * retr), (19, top + 0.5 * a)]
        level = top - 0.07 * pole
    elif kind == "ascending_triangle":
        top = p0 + 8 * a
        anc = [(0, p0), (10, top), (18, top - 6 * a), (26, top), (34, top - 4 * a), (42, top),
               (50, top - 2 * a), (55, top - 0.4 * a), (56, top + 0.8 * a)]
        level = top
    elif kind == "falling_wedge":
        L1 = p0 - 8 * a
        U0 = L1 + 5 * a
        anc = [(0, p0), (20, L1), (28, U0), (36, L1 - 0.96 * a), (44, U0 - 2.56 * a), (52, L1 - 1.92 * a),
               (60, U0 - 5.12 * a), (65, L1 - 2.4 * a), (66, L1 - 0.48 * a)]
        level = L1 - 1.08 * a
    elif kind in ("rounding_bottom", "cup_and_handle"):
        w, depth, rim = 60, 5 * a, p0
        x = np.linspace(-1, 1, w)
        cup = rim - depth * (1 - x ** 2)
        if kind == "rounding_bottom":
            path = np.r_[cup, rim + 0.9 * a]
        else:
            handle = _path([(0, rim), (3, rim - 1.2 * a), (6, rim - 0.4 * a)])
            path = np.r_[cup, handle, rim + 0.9 * a]
        vol = np.r_[np.linspace(1.3, 0.5, w // 2), np.linspace(0.5, 1.4, w - w // 2),
                    np.full(len(path) - w, 0.5)]
        anc, level = None, rim
    elif kind == "v_bottom":
        peak, low = p0 + 1.5 * a, p0 - 7 * a
        reclaim = low + 0.5 * (peak - low)
        anc = [(0, p0), (6, peak), (12, low), (15, low + 2.5 * a), (16, reclaim + 0.6 * a)]
        level = reclaim
    elif kind == "range_breakout":
        top, bot = p0 + 2.5 * a, p0 - 2.5 * a
        anc = [(0, p0), (5, top), (13, bot), (21, top), (29, bot), (37, top), (45, bot), (53, top),
               (58, top - 1.5 * a), (62, top - 0.2 * a), (63, top + 0.9 * a)]
        level = top
    elif kind == "trendline_break":
        h1 = p0 + a
        anc = [(0, p0), (5, h1), (15, h1 - 4 * a), (25, h1 - 1.8 * a), (35, h1 - 5.5 * a),
               (39, h1 - 3.6 * a), (40, h1 - 1.4 * a)]
        level = h1 - 3.15 * a
    # ---------------- bearish shapes (research + long blocker) ----------------
    elif kind == "double_top":
        high = p0 + 7 * a
        neck = high - 3.5 * a
        anc = [(0, p0), (22, high), (31, neck), (40, high - 0.02 * a), (49, neck + 0.3 * a), (50, neck - 0.8 * a)]
        level = neck
    elif kind == "head_shoulders":
        ls = p0 + 6 * a
        neck, hd = ls - 3 * a, ls + 2.2 * a
        anc = [(0, p0), (20, ls), (28, neck), (37, hd), (46, neck - 0.2 * a), (54, ls - 0.1 * a),
               (61, neck + 0.3 * a), (62, neck - 1.0 * a)]
        level = neck - 0.38 * a
    elif kind == "descending_triangle":
        bot = p0 - 8 * a
        anc = [(0, p0), (10, bot), (18, bot + 6 * a), (26, bot), (34, bot + 4 * a), (42, bot),
               (50, bot + 2 * a), (55, bot + 0.4 * a), (56, bot - 0.8 * a)]
        level = bot
    else:
        raise ValueError(f"unknown pattern {kind}")
    if anc is not None:
        path = _path(anc)
    n = len(path)
    vol = np.ones(n) if vol is None else vol[:n]
    vol[-1] = 3.5 if with_volume else 1.0                       # breakout candle relative volume
    _paint(df, at, path, rng, vol)
    if not with_volume:                                         # exactly average volume on the breakout
        i = at + n - 1
        base = float(df["quote_volume"].iloc[i - 30:i].mean())
        df.iloc[i, df.columns.get_loc("quote_volume")] = base
        df.iloc[i, df.columns.get_loc("volume")] = base / float(df["close"].iloc[i])
    return {"kind": kind, "breakout": at + n - 1, "level": float(level), "start": at, "end": at + n - 1,
            "side": "short" if kind in BEARISH else "long"}


def atr_scale(df: pd.DataFrame, n: int = 500) -> float:
    """Median true range of the last n candles — the unit used by plant()."""
    d = df.tail(n)
    tr = np.maximum(d["high"] - d["low"], np.maximum((d["high"] - d["close"].shift()).abs(),
                                                     (d["low"] - d["close"].shift()).abs()))
    return float(tr.median())


def write_universe(data_dir, symbols: dict | list, days: int = 900, seed: int = 0, start: str = "2023-01-01") -> list:
    """Write `<SYMBOL>_15m.parquet` files for an offline universe. `symbols` is a list of names or a
    {name: {"price":..., "ann_vol":..., "base_volume":...}} mapping. BTC is generated first and every
    other coin gets a beta to BTC's returns so cross-coin correlation looks realistic."""
    from pathlib import Path
    out_dir = Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = {s: {} for s in symbols} if isinstance(symbols, (list, tuple, set)) else dict(symbols)
    n = days * 96
    rng = np.random.default_rng(seed)
    btc = gbm_ohlcv(n, "15m", seed=seed, start=start, price=spec.get("BTCUSDT", {}).get("price", 40_000.0),
                    ann_vol=spec.get("BTCUSDT", {}).get("ann_vol", 0.6),
                    base_volume=spec.get("BTCUSDT", {}).get("base_volume", 500.0))
    btc_r = np.log(btc["close"]).diff().fillna(0.0).values
    written = []
    for k, (sym, cfg) in enumerate(spec.items()):
        if sym == "BTCUSDT":
            df = btc
        else:
            own = gbm_ohlcv(n, "15m", seed=seed + 1 + k, start=start, price=cfg.get("price", 10.0 + 7 * k),
                            ann_vol=cfg.get("ann_vol", 0.9), base_volume=cfg.get("base_volume", 20_000.0))
            beta = cfg.get("beta", 0.6 + 0.3 * rng.random())
            r = np.log(own["close"]).diff().fillna(0.0).values + beta * btc_r
            scale = np.exp(np.cumsum(r)) / (own["close"].values / own["close"].values[0])
            df = own.copy()
            for c in ("open", "high", "low", "close"):
                df[c] = own[c].values * scale
            vw = (df["open"] + df["close"]) / 2
            df["quote_volume"] = df["volume"] * vw
            df["taker_buy_quote"] = df["taker_buy_base"] * vw
        path = out_dir / f"{sym}_15m.parquet"
        df[KLINE_COLS].to_parquet(path)
        written.append(path)
    return written
