"""Strategy research library — every strategy speaks one interface and never places orders.

    prepare(raw_ohlcv, ctx) -> dataframe with indicators (recomputed from raw; used by the lookahead test)
    signals(df, ctx, params) -> (entry[bool], exit[bool], weight[float] or None)   decided on candle close

Validation, gating and the standard signal record live in validation.py / audit.py.
Evidence levels follow the review: A research-grade, B reproducible backtests,
C author claims / dry-run only, D reference only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

import indicators as ind
from regime import BARS_PER_YEAR

RAW = ["open", "high", "low", "close", "volume", "quote_volume", "trades",
       "taker_buy_base", "taker_buy_quote"]


def _ctx(ctx: dict, name: str, idx: pd.DatetimeIndex) -> np.ndarray:
    s = ctx.get(name)
    if s is None:
        return np.full(len(idx), np.nan)
    return s.reindex(idx).values


def _vol_target_weight(df: pd.DataFrame, tf: str) -> np.ndarray:
    """Volatility-normalised size: target = trailing 1-year median ATR%, capped at 1x."""
    yr = BARS_PER_YEAR[tf]
    target = df["atr_pct"].rolling(yr, min_periods=100).median()
    return (target / df["atr_pct"]).clip(0.25, 1.0).fillna(0.5).values


class Strategy:
    id = "base"
    name = "base"
    family = ""
    evidence = "D"
    evidence_note = ""
    regime_required = "any"
    space: dict = {}
    stop_atr = 2.0
    tp_atr = None
    max_bars = None
    horizon_bars = 48
    live_eligible = True

    def exec_kw(self, p: dict) -> dict:
        """Backtest exit mechanics for this strategy (see validation.backtest)."""
        return {"stop_atr": p.get("stop_atr", self.stop_atr), "tp_atr": p.get("tp_atr", self.tp_atr),
                "max_bars": p.get("max_bars", self.max_bars)}

    def prepare(self, raw: pd.DataFrame, ctx: dict) -> pd.DataFrame:
        return ind.add_all(raw[[c for c in RAW if c in raw.columns]])

    def signals(self, df, ctx, p):
        raise NotImplementedError


# 1 ---------------------------------------------------------------------------
class EmaAdxBaseline(Strategy):
    id = "trend_ema_adx_v1"
    name = "Trend baseline (EMA50/200 + ADX)"
    family = "trend"
    evidence = "B"
    evidence_note = "Same rules as the Freqtrade CoinAuditTrend strategy; the non-ML benchmark."
    regime_required = "trend"
    space = {"adx_min": [15, 20, 25], "stop_atr": [1.5, 2.0, 3.0]}
    horizon_bars = 30

    def signals(self, df, ctx, p):
        btc = _ctx(ctx, "btc_risk_on", df.index)
        e = ((df["close"] > df["ema50"]) & (df["ema50"] > df["ema200"]) & (df["rsi"] > 50)
             & (df["adx"] > p["adx_min"]) & (df["pdi"] > df["mdi"])).values & (np.nan_to_num(btc, nan=1) == 1)
        x = (df["close"] < df["ema50"]).values
        return e, x, None


# 2 ---------------------------------------------------------------------------
class MaEnsemble(Strategy):
    id = "trend_ma_ensemble_v1"
    name = "Multi-horizon MA trend ensemble"
    family = "trend"
    evidence = "A/B"
    evidence_note = ("bittrends walk-forward study: simple SMA/EMA trend systems historically "
                     "Sharpe ~0.5-1.5 with weaker recent periods.")
    regime_required = "trend"
    space = {"scale": [0.5, 1.0, 2.0], "votes": [2, 3]}
    stop_atr = 3.0
    horizon_bars = 60

    def signals(self, df, ctx, p):
        c = df["close"]
        lens = [max(5, int(n * p["scale"])) for n in (20, 50, 100)]
        above = [(c > ta.sma(c, length=n)).astype(int) for n in lens]
        slope = [(ta.sma(c, length=n).diff(3) > 0).astype(int) for n in lens]
        votes = sum(a & s for a, s in zip(above, slope))
        e = (votes >= p["votes"]).values
        x = (votes <= 1).values
        return e, x, _vol_target_weight(df, ctx["tf"])


# 3 ---------------------------------------------------------------------------
class Donchian(Strategy):
    id = "trend_donchian_v1"
    name = "Donchian breakout"
    family = "trend"
    evidence = "A/B"
    evidence_note = "Donchian breakout ensembles reduced drawdown vs buy & hold in published BTC research."
    regime_required = "trend"
    space = {"n": [20, 55, 100], "stop_atr": [2.0, 3.0]}
    horizon_bars = 60

    def signals(self, df, ctx, p):
        n = p["n"]
        hi = df["high"].rolling(n).max().shift(1)
        lo = df["low"].rolling(max(5, n // 2)).min().shift(1)
        e = (df["close"] > hi).values
        x = (df["close"] < lo).values
        return e, x, _vol_target_weight(df, ctx["tf"])


# 4 ---------------------------------------------------------------------------
class VolManagedMomentum(Strategy):
    id = "momentum_volmanaged_v1"
    name = "Volatility-managed momentum"
    family = "momentum"
    evidence = "A"
    evidence_note = ("2025 studies: plain crypto momentum insignificant due to crashes; "
                     "volatility scaling improved risk-adjusted returns after costs.")
    regime_required = "momentum"
    space = {"lookback": [30, 90, 180], "min_score": [0.0, 0.5]}
    stop_atr = 3.0
    horizon_bars = 40

    def signals(self, df, ctx, p):
        L = p["lookback"]
        c = df["close"]
        mom = c.pct_change(L)
        rv = c.pct_change().rolling(30).std()
        score = mom / (rv * np.sqrt(L))
        btc = ctx.get("btc_close")
        if btc is not None and not ctx.get("is_btc"):
            b = btc.reindex(df.index)
            rs = (mom - b.pct_change(L)).values
        else:
            rs = np.zeros(len(df)) + 1e-9
        yr = BARS_PER_YEAR[ctx["tf"]]
        crash = (rv > rv.rolling(yr, min_periods=100).quantile(0.9)).values   # top trailing vol decile
        e = (score > p["min_score"]).values & (rs > 0) & ~crash
        x = (mom < 0).values | (rs < 0) | crash
        target = rv.rolling(BARS_PER_YEAR[ctx["tf"]], min_periods=100).median()
        w = (target / rv).clip(0.25, 1.0).fillna(0.5).values
        return e, x, w


# 5 ---------------------------------------------------------------------------
PULLBACK_MODULES = ["trend_filter", "btc_regime", "safe_dip", "pump_guard", "volume_confirmation", "ewo_guard"]


class Pullback(Strategy):
    id = "pullback_nfi_modules_v1"
    name = "Trend pullback (NFI-inspired modules)"
    family = "pullback"
    evidence = "C"
    evidence_note = ("Modules extracted from NostalgiaForInfinity concepts (not its rule tree); "
                     "NFI results are author-provided, not audited.")
    regime_required = "trend_pullback"
    space = {"rsi_th": [30, 35, 40], "stop_atr": [1.5, 2.5]}
    max_bars = 24
    horizon_bars = 24
    disabled_modules: tuple = ()

    def modules(self, df, ctx, p) -> dict:
        c = df["close"]
        htf_c, htf_e = _ctx(ctx, "htf_close", df.index), _ctx(ctx, "htf_ema200", df.index)
        base_trend = (c > df["ema200"]).values
        trend = np.where(np.isnan(htf_e), base_trend, htf_c > htf_e)
        btc = np.nan_to_num(_ctx(ctx, "btc_risk_on", df.index), nan=1) == 1
        dip = ((df["rsi"] < p["rsi_th"]) & ((df["ema20"] - c) / df["atr"] >= 1.0)).values
        rng24 = df["high"].rolling(24).max() / df["low"].rolling(24).min() - 1
        pump = (rng24 < 8 * df["atr_pct"]).values
        vol = ((df["vol_ratio"] > 0.7) & (df["taker_ratio"].rolling(3).mean() > 0.42)).values
        ewo = ((ta.ema(c, length=5) - ta.ema(c, length=35)) / c * 100)
        ewo_ok = (ewo > ewo.rolling(200, min_periods=50).quantile(0.05)).values
        return {"trend_filter": trend, "btc_regime": btc, "safe_dip": dip,
                "pump_guard": pump, "volume_confirmation": vol, "ewo_guard": ewo_ok}

    def signals(self, df, ctx, p, disabled=()):
        m = self.modules(df, ctx, p)
        e = np.ones(len(df), bool)
        for k, v in m.items():
            if k not in disabled and k not in self.disabled_modules:
                e &= np.nan_to_num(v.astype(float), nan=0).astype(bool)
        x = ((df["close"] > df["ema20"]) | (df["rsi"] > 65)).values   # profit protection / reversion done
        return e, x, None


# 6 ---------------------------------------------------------------------------
def _ewo(c):
    return (ta.ema(c, length=5) - ta.ema(c, length=35)) / c * 100


def _btc_ok(ctx, idx):
    return np.nan_to_num(_ctx(ctx, "btc_risk_on", idx), nan=1) == 1


class SmaOffset(Strategy):
    id = "sma_offset_v1"
    name = "SMA Offset (NotAnotherSMAOffset-style)"
    family = "dip"
    evidence = "C"
    evidence_note = ("Ranks high in community leagues (Freqle); results are league/author backtests. "
                     "HO/Protect variant: BTC filter + pump guard added.")
    regime_required = "dip"
    space = {"low_offset": [0.96, 0.975, 0.99], "ewo_high": [2.0, 4.0]}
    stop_atr = 3.0
    horizon_bars = 60

    def exec_kw(self, p):
        return {"stop_atr": 3.0, "max_bars": 120, "trail": (0.02, 0.01),
                "roi": [(0, 0.08), (30, 0.03), (60, 0.005)]}

    def signals(self, df, ctx, p):
        c = df["close"]
        ma = ta.sma(c, length=14)
        ewo, rsi_f = _ewo(c), ta.rsi(c, length=4)
        rng24 = df["high"].rolling(24).max() / df["low"].rolling(24).min() - 1
        protect = _btc_ok(ctx, df.index) & (rng24 < 8 * df["atr_pct"]).values
        e1 = (c < ma * p["low_offset"]) & (ewo > p["ewo_high"]) & (df["rsi"] < 50) & (rsi_f < 35)
        e2 = (c < ma * p["low_offset"]) & (ewo < -8) & (rsi_f < 25)
        x = ((c > ta.sma(c, length=20) * 1.01) & (df["rsi"] > 50)).values
        return (e1 | e2).values & protect, x, None


# 7 ---------------------------------------------------------------------------
class ClucBB(Strategy):
    id = "bincluc_bb_v1"
    name = "BinCluc (BinHV45 + ClucMay Bollinger)"
    family = "dip"
    evidence = "C"
    evidence_note = "CombinedBinHAndCluc / BinClucMad family — common in league top ranks; author/league backtests."
    regime_required = "dip"
    space = {"cluc_mult": [0.975, 0.985, 0.99], "stop_atr": [2.0, 3.0]}
    horizon_bars = 36

    def exec_kw(self, p):
        return {"stop_atr": p["stop_atr"], "max_bars": 72, "roi": [(0, 0.05), (24, 0.02), (48, 0.0)]}

    def signals(self, df, ctx, p):
        c, lo = df["close"], df["low"]
        bb40 = ta.bbands(c, length=40, std=2)
        mid40, low40 = bb40.iloc[:, 1], bb40.iloc[:, 0]
        bbdelta = (mid40 - low40).abs()
        closedelta = (c - c.shift()).abs()
        tail = (c - lo).abs()
        binh = ((low40.shift() > 0) & (bbdelta > c * 0.008) & (closedelta > c * 0.0175)
                & (tail < bbdelta * 0.25) & (c < low40.shift()) & (c <= c.shift()))
        bb20 = ta.bbands(c, length=20, std=2)
        low20, mid20 = bb20.iloc[:, 0], bb20.iloc[:, 1]
        cluc = (c < df["ema50"]) & (c < p["cluc_mult"] * low20) & (df["volume"] < df["volume"].rolling(30).mean() * 20)
        e = (binh | cluc).values & _btc_ok(ctx, df.index)
        x = (c > mid20).values
        return e, x, None


# 8 ---------------------------------------------------------------------------
class ElliotEwo(Strategy):
    id = "elliot_ewo_v1"
    name = "ElliotV-style EWO offset"
    family = "dip"
    evidence = "C"
    evidence_note = "ElliotV5/V8 family: EMA offset entries gated by the Elliott Wave Oscillator; league backtests only."
    regime_required = "dip"
    space = {"low_offset": [0.97, 0.978, 0.985], "ewo_high": [3.0, 5.6]}
    horizon_bars = 48

    def exec_kw(self, p):
        return {"stop_atr": 3.0, "max_bars": 120, "trail": (0.03, 0.01),
                "roi": [(0, 0.20), (30, 0.04), (90, 0.0)]}

    def signals(self, df, ctx, p):
        c = df["close"]
        ewo = _ewo(c)
        base = ta.ema(c, length=17)
        e1 = (c < base * p["low_offset"]) & (ewo > p["ewo_high"]) & (df["rsi"] < 65)
        e2 = (c < base * p["low_offset"]) & (ewo < -19)
        x = (c > ta.ema(c, length=49) * 1.006).values
        return (e1 | e2).values & _btc_ok(ctx, df.index), x, None


# 9 ---------------------------------------------------------------------------
class Confluence(Strategy):
    id = "confluence_score_v1"
    name = "Multi-indicator confluence score (TrendRider-style)"
    family = "trend"
    evidence = "C"
    evidence_note = ("Scoring of EMA stack, RSI, ADX, volume, Bollinger, MACD with ATR stops and cascading "
                     "early-loss cuts; public dry-run dashboards, not audited.")
    regime_required = "trend"
    space = {"k": [4, 5], "stop_atr": [2.0, 3.0]}
    horizon_bars = 40

    def exec_kw(self, p):
        return {"stop_atr": p["stop_atr"], "max_bars": 60, "trail": (0.05, 0.025),
                "early_cut": [(3, 1.0), (6, 0.5), (12, 0.0)]}      # 12 candles flat -> "deadfish" exit

    def score(self, df):
        c = df["close"]
        parts = [(df["ema20"] > df["ema50"]) & (df["ema50"] > df["ema200"]),
                 df["rsi"].between(50, 70),
                 (df["adx"] > 20) & (df["pdi"] > df["mdi"]),
                 df["vol_ratio"] > 1.2,
                 (c > ta.sma(c, length=20)) & (df["bbw"] > df["bbw"].shift(5)),
                 (df["macd_hist"] > 0) & (df["macd_hist"] > df["macd_hist"].shift())]
        return sum(p_.astype(int) for p_ in parts)

    def signals(self, df, ctx, p):
        sc = self.score(df)
        return (sc >= p["k"]).values & _btc_ok(ctx, df.index), (sc <= 2).values, None


# 10 --------------------------------------------------------------------------
class EmaCrossFunding(Strategy):
    id = "ema_cross_funding_v1"
    name = "EMA cross + funding-rate filter (btc-strategy-lab winner)"
    family = "trend"
    evidence = "B"
    evidence_note = ("wiktorj137/btc-strategy-lab: of 42 public strategies on 8 years of BTC 1h, only 3 beat "
                     "buy & hold; the winner was a simple EMA cross with a funding filter.")
    regime_required = "any"
    space = {"fast": [12, 21], "slow": [55, 100], "fund_max": [0.0001, 0.0003]}
    stop_atr = 3.0
    horizon_bars = 80

    def signals(self, df, ctx, p):
        c = df["close"]
        f, s_ = ta.ema(c, length=p["fast"]), ta.ema(c, length=p["slow"])
        fund = _ctx(ctx, "funding_3d", df.index)
        fund_ok = np.where(np.isnan(fund), True, fund < p["fund_max"])   # crowded longs -> stand aside
        return (f > s_).values & fund_ok, (f < s_).values, None


# 11 --------------------------------------------------------------------------
class IctSweepFvg(Strategy):
    id = "ict_sweep_fvg_v1"
    name = "Liquidity sweep + FVG, 50% entry (ICT / 'Katana'-style)"
    family = "discretionary-mechanised"
    evidence = "C/D"
    evidence_note = ("Popular on X with 70%+ win-rate claims (mostly indices/futures, unverified). "
                     "Mechanised here so it can be tested like everything else.")
    regime_required = "any"
    space = {"n": [20, 40], "tp_r": [2.0, 3.0]}
    horizon_bars = 30

    def exec_kw(self, p):
        return {"tp_r": p["tp_r"], "max_bars": 30, "limit_bars": 3}

    def signals(self, df, ctx, p):
        c, h, lo, o = df["close"], df["high"], df["low"], df["open"]
        swing = lo.rolling(p["n"]).min().shift(1)
        sweep = (lo < swing) & (c > swing)                      # wick through liquidity, close back
        recent = sweep.astype(int).rolling(5).max() == 1
        fvg = (lo > h.shift(2)) & (c > o)                       # bullish fair-value gap on displacement
        e = (fvg & recent).values & _btc_ok(ctx, df.index)
        sweep_low = lo.rolling(6).min()
        limit = ((sweep_low + h) / 2).values                    # 50% of the displacement range
        stop = (sweep_low - 0.1 * df["atr"]).values
        return e, np.zeros(len(df), bool), None, {"limit_px": limit, "stop_px": stop}


# 12 --------------------------------------------------------------------------
class DeepDrawdownReclaim(Strategy):
    id = "deep_drawdown_reclaim_v1"
    name = "Deep drawdown reclaim (-80..-90% from ATH)"
    family = "low-cap rebound"
    evidence = "D"
    evidence_note = ("'Boring' low-cap rule from X: buy after -85-90% from ATH with confirmation, aim 1.5-3x. "
                     "Narrative/community confirmation is not available here — price reclaim is used instead. "
                     "ATH = highest high in the loaded history (full history on 1d).")
    regime_required = "any"
    space = {"dd": [0.80, 0.85, 0.90], "tp_pct": [0.5, 1.0]}
    horizon_bars = 90

    def exec_kw(self, p):
        return {"stop_pct": 0.25, "tp_pct": p["tp_pct"], "max_bars": 90}   # +TP / -25% / time exit

    def signals(self, df, ctx, p):
        c = df["close"]
        dd = c / df["high"].cummax() - 1
        reclaim = c > df["high"].rolling(20).max().shift(1)
        age_ok = np.arange(len(df)) >= 90
        return (dd <= -p["dd"]).values & reclaim.values & age_ok, np.zeros(len(df), bool), None


# =============================================================================
# Chart-pattern & price-action modules (see patterns.py). Each is its own strategy with its
# own parameters and validation — they are never blended into one blind score.
# =============================================================================
import patterns as pt  # noqa: E402  (after the base classes on purpose)

_CACHE: dict = {}


def _pat(df, key, fn):
    """Cache pivots/formations per dataframe (formations don't depend on entry parameters).
    The entry keeps a reference to the frame and checks identity, so a recycled object id can never
    return another coin's formations (v6 fix)."""
    k = (id(df), len(df), key)
    hit = _CACHE.get(k)
    if hit is not None and hit[0] is df:
        return hit[1]
    if len(_CACHE) > 400:
        _CACHE.clear()
    _CACHE[k] = (df, fn())
    return _CACHE[k][1]


def _piv(df):
    return _pat(df, "pivots", lambda: pt.pivots(df))


class PatternStrategy(Strategy):
    family = "chart pattern"
    evidence = "C"
    evidence_note = "Classical chart pattern; textbook descriptions, not verified edge — validated here per coin."
    regime_required = "any"
    space = {"vol_mult": [1.2, 1.5], "entry": ["close", "retest"], "stop_mode": ["pattern", "tight"]}
    horizon_bars = 40
    need_bos = True

    def formations(self, df):
        raise NotImplementedError

    def exec_kw(self, p):
        return {"max_bars": 40, "limit_bars": 5}

    def signals(self, df, ctx, p):
        hi, _ = _piv(df)
        setups = _pat(df, self.id, lambda: self.formations(df))
        (e, sp, tp, lp), _ = pt.trigger(df, setups, vol_mult=p["vol_mult"], entry=p["entry"],
                                        need_bos=self.need_bos, hi_idx=hi, stop_mode=p.get("stop_mode", "pattern"))
        return e, np.zeros(len(df), bool), None, {"stop_px": sp, "tp_px": tp,
                                                  "limit_px": lp if p["entry"] == "retest" else None}


class DoubleTripleBottom(PatternStrategy):
    id = "double_triple_bottom_v1"
    name = "Double / triple bottom (W) neckline breakout"

    def formations(self, df):
        hi, lo = _piv(df)
        return pt.double_bottom(df, hi, lo) + pt.double_bottom(df, hi, lo, triple=True)


class InverseHeadShoulders(PatternStrategy):
    id = "inverse_head_shoulders_v1"
    name = "Inverse head & shoulders neckline breakout"

    def formations(self, df):
        hi, lo = _piv(df)
        return pt.head_shoulders(df, hi, lo, side="long")


class RangeBreakout(PatternStrategy):
    id = "range_breakout_v1"
    name = "Rectangle / range breakout"
    regime_required = "any"

    def formations(self, df):
        hi, lo = _piv(df)
        return [pt._setup("range_breakout", b["start"], b["valid_from"], b["top"],
                          b["bottom"] + 0.5 * b["height"], b["top"] + b["height"], expiry=b["expiry"])
                for b in pt.range_box(df, hi, lo)]


class BullFlag(PatternStrategy):
    id = "bull_flag_pennant_v1"
    name = "Bull flag / pennant / high tight flag"
    family = "continuation pattern"
    regime_required = "trend"
    need_bos = False

    def formations(self, df):
        return pt.flags(df, side="long")


class TriangleWedge(PatternStrategy):
    id = "triangle_wedge_breakout_v1"
    name = "Ascending / symmetrical triangle, falling wedge breakout"
    family = "continuation pattern"
    need_bos = False

    def formations(self, df):
        hi, lo = _piv(df)
        return [s for s in pt.converging(df, hi, lo) if s.get("side") == "long"]


class RoundingCup(PatternStrategy):
    id = "rounding_cup_handle_v1"
    name = "Rounding bottom (U) / cup & handle"

    def formations(self, df):
        return pt.rounding(df, 60) + pt.rounding(df, 60, with_handle=True)


class VBottom(PatternStrategy):
    id = "v_bottom_reclaim_v1"
    name = "V-bottom reclaim (BOS + volume, dead-cat filter)"
    evidence = "C/D"
    space = {"vol_mult": [1.3, 1.8], "entry": ["close", "retest"], "stop_mode": ["pattern", "tight"]}

    def formations(self, df):
        hi, _ = _piv(df)
        return pt.v_bottom(df, hi)


class TrendlineRetest(PatternStrategy):
    id = "trendline_break_retest_v1"
    name = "Downtrend-line break & retest"
    space = {"vol_mult": [1.2, 1.5], "entry": ["retest", "close"], "stop_mode": ["pattern", "tight"]}

    def formations(self, df):
        hi, lo = _piv(df)
        return pt.descending_trendline(df, hi, lo)


class RangeSupport(Strategy):
    id = "range_support_v1"
    name = "Range trading: buy validated support, sell resistance"
    family = "range"
    evidence = "C"
    evidence_note = "Buy near validated range support, target resistance; never the middle of the range."
    regime_required = "range"
    space = {"zone": [0.2, 0.3]}
    horizon_bars = 30

    def exec_kw(self, p):
        return {"max_bars": 40}

    def signals(self, df, ctx, p):
        hi, lo = _piv(df)
        boxes = _pat(df, "boxes", lambda: pt.range_box(df, hi, lo))
        o, c, L, a = (df[k].values for k in ("open", "close", "low", "atr"))
        n = len(df)
        e, sp, tp = np.zeros(n, bool), np.full(n, np.nan), np.full(n, np.nan)
        for b in boxes:
            for t in range(b["valid_from"], min(b["expiry"], n - 1) + 1):
                if c[t] > b["top"] + 0.5 * a[t] or c[t] < b["bottom"] - 0.5 * a[t]:
                    break                                       # range broken -> stop range trading it
                if c[t] <= b["bottom"] + p["zone"] * b["height"] and c[t] > o[t] and c[t] > c[t - 1] \
                        and L[t] >= b["bottom"] - 0.5 * a[t]:
                    e[t], sp[t], tp[t] = True, b["bottom"] - 0.5 * a[t], b["top"] - 0.1 * b["height"]
        return e, np.zeros(n, bool), None, {"stop_px": sp, "tp_px": tp}


class ChannelBounce(Strategy):
    id = "channel_bounce_v1"
    name = "Ascending channel: bounce off the lower line"
    family = "channel"
    evidence = "C"
    evidence_note = "Parallel rising channel with >= 2 touches per line; buy the lower line, target the upper."
    regime_required = "trend_pullback"
    space = {"touch_atr": [0.3, 0.6]}

    def exec_kw(self, p):
        return {"max_bars": 30}

    def signals(self, df, ctx, p):
        hi, lo = _piv(df)
        chans = [x for x in _pat(df, "conv", lambda: pt.converging(df, hi, lo)) if "channel" in x]
        o, c, L, a = (df[k].values for k in ("open", "close", "low", "atr"))
        n = len(df)
        e, sp, tp = np.zeros(n, bool), np.full(n, np.nan), np.full(n, np.nan)
        for ch in chans:
            (ui, up, us), (li, lp_, ls) = ch["channel"]
            for t in range(ch["valid_from"], min(ch["expiry"], n - 1) + 1):
                low_line, up_line = lp_ + ls * (t - li), up + us * (t - ui)
                if c[t] < low_line - 0.5 * a[t]:
                    break
                if L[t] <= low_line + p["touch_atr"] * a[t] and c[t] > low_line and c[t] > o[t]:
                    e[t], sp[t], tp[t] = True, low_line - 0.5 * a[t], up_line
        return e, np.zeros(n, bool), None, {"stop_px": sp, "tp_px": tp}


class CandleAtLevel(Strategy):
    id = "candle_at_key_level_v1"
    name = "Bullish candlestick at a key level (support / lower BB)"
    family = "candlestick"
    evidence = "C"
    evidence_note = "Engulfing, hammer, morning star, piercing, tweezer, harami… only at support — never mid-range."
    regime_required = "any"
    space = {"tp_r": [1.5, 2.0], "level_atr": [0.3, 0.6]}
    horizon_bars = 20

    def exec_kw(self, p):
        return {"tp_r": p["tp_r"], "max_bars": 30}

    def signals(self, df, ctx, p):
        hi, lo = _piv(df)
        cd = _pat(df, "candles", lambda: pt.candles(df))
        bull = np.zeros(len(df), bool)
        for k in pt.BULL_CANDLES:
            bull |= cd[k]
        L, a = df["low"].values, df["atr"].values
        bb = ta.bbands(df["close"], length=20, std=2).iloc[:, 0].values
        n = len(df)
        at_level = np.zeros(n, bool)
        j1 = np.searchsorted(lo + pt.K, np.arange(n), side="right")       # pivots confirmed by t
        j0 = np.searchsorted(lo, np.arange(n) - 100, side="left")         # ... and not older than 100 bars
        for t in np.flatnonzero(bull):
            sup = lo[j0[t]:j1[t]]
            near = len(sup) and np.min(np.abs(L[t] - L[sup])) <= p["level_atr"] * a[t]
            at_level[t] = bool(near) or (np.isfinite(bb[t]) and L[t] <= bb[t])
        stop = L - 0.2 * a
        return bull & at_level, np.zeros(n, bool), None, {"stop_px": stop}


class SmcConfluence(Strategy):
    id = "smc_ob_sweep_bos_fvg_v1"
    name = "SMC: liquidity sweep → BOS/CHoCH + FVG → order-block retest"
    family = "smart money concepts"
    evidence = "C/D"
    evidence_note = ("The confluence chain traders post on X (sweep equal lows → bullish BOS/CHoCH with a fair-value "
                     "gap → buy the order-block retest). Win-rate claims are unverified; tested mechanically here.")
    regime_required = "any"
    space = {"tp_r": [2.0, 3.0], "divergence": [False, True]}
    horizon_bars = 30

    def exec_kw(self, p):
        return {"tp_r": p["tp_r"], "max_bars": 40, "limit_bars": 10}

    def signals(self, df, ctx, p):
        hi, lo = _piv(df)
        sets = _pat(df, f"smc{p['divergence']}", lambda: pt.smc_setups(df, hi, lo, p["divergence"]))
        n, a = len(df), df["atr"].values
        e, sp, lp = np.zeros(n, bool), np.full(n, np.nan), np.full(n, np.nan)
        for s_ in sets:
            t = s_["t"]
            e[t], lp[t], sp[t] = True, s_["ob_top"], min(s_["ob_low"], s_["sweep_low"]) - 0.1 * a[t]
        return e & _btc_ok(ctx, df.index), np.zeros(n, bool), None, {"stop_px": sp, "limit_px": lp}


class FailureSwing(Strategy):
    id = "rsi_failure_swing_v1"
    name = "Bullish RSI failure swing"
    family = "reversal"
    evidence = "C"
    evidence_note = "Momentum fails to make a new low: RSI <30, higher RSI low above 30, break of the RSI swing high."
    regime_required = "any"
    space = {"tp_r": [1.5, 2.5]}
    horizon_bars = 30

    def exec_kw(self, p):
        return {"tp_r": p["tp_r"], "max_bars": 40}

    def signals(self, df, ctx, p):
        ev = _pat(df, "fswing", lambda: pt.failure_swing(df))
        n = len(df)
        e, sp = np.zeros(n, bool), np.full(n, np.nan)
        for x in ev:
            if x["t"] < n:
                e[x["t"]], sp[x["t"]] = True, x["stop"]
        return e, np.zeros(n, bool), None, {"stop_px": sp}


class SqueezeBreakout(Strategy):
    id = "squeeze_breakout_v1"
    name = "Momentum breakout after volatility compression"
    family = "momentum"
    evidence = "B/C"
    evidence_note = "New 20-candle high right after Bollinger-width compression, on high relative volume."
    regime_required = "momentum"
    space = {"pct": [0.10, 0.20], "vol_mult": [1.3, 1.8]}
    horizon_bars = 30

    def exec_kw(self, p):
        return {"max_bars": 40, "trail": (0.05, 0.025)}

    def signals(self, df, ctx, p):
        bbw = df["bbw"]
        squeezed = (bbw <= bbw.rolling(120, min_periods=60).quantile(p["pct"])).rolling(5).max() == 1
        brk = df["close"] > df["high"].rolling(20).max().shift(1)
        e = (squeezed & brk & (df["vol_ratio"] >= p["vol_mult"])).values & _btc_ok(ctx, df.index)
        stop = (df["low"].rolling(10).min() - 0.2 * df["atr"]).values
        return e, np.zeros(len(df), bool), None, {"stop_px": stop}


class VwapReversion(Strategy):
    id = "vwap_reversion_v1"
    name = "Mean reversion to rolling VWAP after an extreme"
    family = "mean reversion"
    evidence = "C"
    evidence_note = "Stretched > z standard deviations below a 50-candle VWAP, then a reversal candle; target = VWAP."
    regime_required = "dip"
    space = {"z": [2.0, 2.5]}
    horizon_bars = 20

    def exec_kw(self, p):
        return {"max_bars": 30}

    def signals(self, df, ctx, p):
        tp_ = (df["high"] + df["low"] + df["close"]) / 3
        vwap = (tp_ * df["volume"]).rolling(50).sum() / df["volume"].rolling(50).sum()
        dev = df["close"] - vwap
        z = dev / dev.rolling(50).std()
        rev = df["close"] > df["high"].shift(1)
        e = ((z.shift(1) < -p["z"]) & rev).values
        stop = (df["low"].rolling(5).min() - 0.2 * df["atr"]).values
        return e, np.zeros(len(df), bool), None, {"stop_px": stop, "tp_px": vwap.values}


PATTERN_LIBRARY = [DoubleTripleBottom(), InverseHeadShoulders(), RangeBreakout(), RangeSupport(), BullFlag(),
                   TriangleWedge(), RoundingCup(), VBottom(), TrendlineRetest(), ChannelBounce(),
                   CandleAtLevel(), SmcConfluence(), FailureSwing(), SqueezeBreakout(), VwapReversion()]


LIBRARY = [EmaAdxBaseline(), MaEnsemble(), Donchian(), VolManagedMomentum(), Pullback(),
           SmaOffset(), ClucBB(), ElliotEwo(), Confluence(), EmaCrossFunding(), IctSweepFvg(),
           DeepDrawdownReclaim()] + PATTERN_LIBRARY


def select(filters: list | None) -> list:
    """Subset of the library matching any of the filters (strategy id, family or a name fragment);
    None or an empty list = the whole library."""
    if not filters:
        return list(LIBRARY)
    fl = [f.lower() for f in filters]
    out = [s for s in LIBRARY if any(f == s.id.lower() or f == s.family.lower() or f in s.name.lower()
                                     or f in s.id.lower() for f in fl)]
    return out or list(LIBRARY)


def ablation(strategy: Pullback, df, ctx, params, hold_start: int) -> list[dict]:
    """Remove one module at a time; keep a module only if removing it lowers the
    median fold return on the development period (holdout untouched)."""
    from validation import backtest
    folds = np.array_split(np.arange(int(hold_start * 0.4), hold_start), 5)

    def med(disabled):
        e, x, w = strategy.signals(df, ctx, params, disabled=disabled)[:3]
        t = backtest(df, e, x, weight=w, **strategy.exec_kw(params))
        rets = []
        for f in folds:
            m = (t.entry_i >= f[0]) & (t.exit_i <= f[-1])
            rets.append(float(np.prod(1 + t.ret[m]) - 1))
        return float(np.median(rets)), int(len(t.ret))

    base, nb = med(())
    rows = [{"module": "ALL (baseline)", "median_fold_return_pct": round(base * 100, 2), "trades": nb, "keep": True}]
    for mod in PULLBACK_MODULES:
        r, n = med((mod,))
        rows.append({"module": f"without {mod}", "median_fold_return_pct": round(r * 100, 2),
                     "trades": n, "keep": r < base})
    return rows
