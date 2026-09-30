"""Audit checks. Each returns a dict:
{name, score 0-100, status pass|warn|fail, notes [str], metrics {k: v}}"""
from __future__ import annotations

import numpy as np
import pandas as pd

import indicators as ind

FEE = 0.001        # Binance spot taker fee per side (0.1%, no BNB discount)
SLIPPAGE = 0.0005  # assumed slippage per side


def _status(score: float) -> str:
    return "pass" if score >= 65 else ("warn" if score >= 40 else "fail")


def _result(name, score, notes, metrics):
    score = float(np.clip(score, 0, 100))
    return {"name": name, "score": round(score, 1), "status": _status(score),
            "notes": notes, "metrics": metrics}


def _band(x, bands):
    """bands: list of (threshold, score) descending thresholds."""
    for th, sc in bands:
        if x >= th:
            return sc
    return bands[-1][1] if bands else 0


# ---------------------------------------------------------------- liquidity
def liquidity(ticker: dict, book: dict) -> dict:
    qv = float(ticker["quoteVolume"])
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    notes = []
    if not bids or not asks:
        return _result("Liquidity", 0, ["Order book is empty."], {"quote_volume_24h": qv})
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * 1e4
    depth_bid = sum(p * q for p, q in bids if p >= mid * 0.99)
    depth_ask = sum(p * q for p, q in asks if p <= mid * 1.01)
    depth = depth_bid + depth_ask

    s_vol = _band(qv, [(100e6, 100), (25e6, 85), (5e6, 65), (1e6, 40), (250e3, 20), (0, 0)])
    s_spr = 100 if spread_bps <= 3 else 85 if spread_bps <= 8 else 60 if spread_bps <= 20 else 30 if spread_bps <= 50 else 5
    s_dep = _band(depth, [(2e6, 100), (500e3, 80), (100e3, 55), (25e3, 30), (0, 5)])
    score = 0.45 * s_vol + 0.30 * s_spr + 0.25 * s_dep

    notes.append(f"24h volume ${qv:,.0f}; spread {spread_bps:.1f} bps; ±1% book depth ${depth:,.0f}.")
    if qv < 1e6:
        notes.append("Volume under $1M/day — slippage and manipulation risk are high.")
    if spread_bps > 20:
        notes.append("Wide spread eats a large share of any short-term edge.")
    imb = depth_bid / depth if depth else 0.5
    notes.append(f"Book imbalance within ±1%: {imb:.0%} bids / {1 - imb:.0%} asks.")
    return _result("Liquidity", score, notes, {
        "quote_volume_24h": round(qv), "spread_bps": round(spread_bps, 2),
        "depth_1pct_usd": round(depth), "book_bid_share": round(imb, 3)})


# -------------------------------------------------------------------- trend
def _tf_trend(df: pd.DataFrame, long_n: int) -> tuple[float, dict]:
    last = df.iloc[-1]
    slow = f"ema{long_n}"
    pts = 0.0
    pts += 25 if last["close"] > last["ema50"] else 0
    pts += 25 if last["ema50"] > last[slow] else 0
    slope = df["ema50"].iloc[-1] / df["ema50"].iloc[-6] - 1
    pts += 20 if slope > 0 else 0
    if last["adx"] >= 20 and last["pdi"] > last["mdi"]:
        pts += 30
    elif last["adx"] < 20:
        pts += 12  # no strong trend either way
    return pts, {"close_gt_ema50": bool(last["close"] > last["ema50"]),
                 f"ema50_gt_{slow}": bool(last["ema50"] > last[slow]),
                 "ema50_slope_5bars_pct": round(slope * 100, 2),
                 "adx": round(float(last["adx"]), 1),
                 "di_bullish": bool(last["pdi"] > last["mdi"])}


def trend(d1: pd.DataFrame, h4: pd.DataFrame, h1: pd.DataFrame) -> dict:
    s1, m1 = _tf_trend(d1, 200 if d1["ema200"].notna().iloc[-1] else 50)
    s4, m4 = _tf_trend(h4, 200)
    sh, mh = _tf_trend(h1, 200)
    score = 0.45 * s1 + 0.35 * s4 + 0.20 * sh
    lab = lambda s: "up" if s >= 70 else ("mixed" if s >= 40 else "down")
    notes = [f"Daily trend {lab(s1)} ({s1:.0f}), 4h {lab(s4)} ({s4:.0f}), 1h {lab(sh)} ({sh:.0f})."]
    if s1 >= 70 and sh < 40:
        notes.append("Higher-timeframe uptrend with a short-term pullback — potential entry area if it holds.")
    if s1 < 40 and sh >= 70:
        notes.append("Short-term bounce inside a daily downtrend — counter-trend, treat with caution.")
    return _result("Trend (multi-timeframe)", score, notes, {"1d": m1, "4h": m4, "1h": mh})


# ----------------------------------------------------------------- momentum
def momentum(h4: pd.DataFrame, d1: pd.DataFrame, tf: str = "4h") -> dict:
    last = h4.iloc[-1]
    r = float(last["rsi"])
    rd = float(d1["rsi"].iloc[-1])
    notes = []
    if 50 <= r <= 68:
        s_rsi = 100
    elif 68 < r <= 78:
        s_rsi = 55; notes.append(f"{tf} RSI {r:.0f} is stretched — chasing risk.")
    elif r > 78:
        s_rsi = 25; notes.append(f"{tf} RSI {r:.0f} is overbought — wait for a reset.")
    elif 40 <= r < 50:
        s_rsi = 55
    elif 30 <= r < 40:
        s_rsi = 35
    else:
        s_rsi = 25; notes.append(f"{tf} RSI {r:.0f} is oversold — possible bounce, but momentum is negative.")
    hist = h4["macd_hist"]
    s_macd = (50 if hist.iloc[-1] > 0 else 0) + (50 if hist.iloc[-1] > hist.iloc[-3] else 0)
    roc = float(h4["close"].iloc[-1] / h4["close"].iloc[-21] - 1)
    s_roc = float(np.clip(50 + roc * 500, 0, 100))
    score = 0.4 * s_rsi + 0.35 * s_macd + 0.25 * s_roc
    notes.insert(0, f"{tf} RSI {r:.0f}, daily RSI {rd:.0f}; MACD histogram "
                    f"{'positive' if hist.iloc[-1] > 0 else 'negative'} and "
                    f"{'rising' if hist.iloc[-1] > hist.iloc[-3] else 'falling'}; "
                    f"20-bar {tf} change {roc:+.1%}.")
    return _result("Momentum", score, notes, {
        "rsi_tf": round(r, 1), "rsi_1d": round(rd, 1),
        "macd_hist_tf": float(hist.iloc[-1]), "roc_20_pct": round(roc * 100, 2)})


# --------------------------------------------------------------- volatility
def volatility(d1: pd.DataFrame, h1: pd.DataFrame | None = None) -> dict:
    atr_pct = float(d1["atr_pct"].iloc[-1])
    rets = d1["close"].pct_change().dropna()
    rv = float(rets.tail(30).std() * np.sqrt(365))
    rv90 = float(rets.tail(90).std() * np.sqrt(365))
    mdd90 = ind.max_drawdown(d1["close"].tail(90))
    notes = [f"Daily ATR {atr_pct:.1%}; 30d annualised vol {rv:.0%} (90d {rv90:.0%}); "
             f"90d max drawdown {mdd90:.0%}."]
    # sweet spot: enough movement to profit after fees, not a casino
    if atr_pct < 0.015:
        s = 45; notes.append("Very low volatility — moves may not cover fees on short horizons.")
    elif atr_pct <= 0.06:
        s = 90
    elif atr_pct <= 0.10:
        s = 60; notes.append("High volatility — size positions smaller.")
    else:
        s = 25; notes.append("Extreme volatility — stop-outs and gaps are likely.")
    if mdd90 < -0.5:
        s -= 20; notes.append("Lost over half its value within 90 days.")
    if rv > rv90 * 1.5:
        notes.append("Volatility is expanding sharply versus the 90-day norm.")
    metrics = {"atr_pct_1d": round(atr_pct * 100, 2), "vol_30d_ann_pct": round(rv * 100, 1),
               "vol_90d_ann_pct": round(rv90 * 100, 1), "max_dd_90d_pct": round(mdd90 * 100, 1)}
    if h1 is not None and len(h1) > 24 * 60:
        from regime import har_rv_forecast
        har = har_rv_forecast(h1["close"], 24)
        if har.get("available"):
            metrics["har_rv"] = har
            notes.append(f"HAR-RV forecast for the next day: {har['forecast_daily_vol_pct']:.2f}% move "
                         f"(today {har['today_rv_vol_pct']:.2f}%, 22-day average {har['avg22_vol_pct']:.2f}%; "
                         f"fit R² {har['r2']:.2f}). Size stops for at least that.")
            if har["forecast_daily_vol_pct"] > 1.3 * har["avg22_vol_pct"]:
                s -= 10; notes.append("Realised-volatility model expects a volatile session — reduce size.")
    return _result("Volatility & risk", s, notes, metrics)


# ------------------------------------------------------- relative strength
def relative_strength(d1: pd.DataFrame, btc_d1: pd.DataFrame | None, is_btc: bool) -> dict:
    b = btc_d1 if btc_d1 is not None else d1
    bl = b.iloc[-1]
    btc_ema_long = bl["ema200"] if pd.notna(bl["ema200"]) else bl["ema50"]
    btc_risk_on = bool(bl["close"] > btc_ema_long and bl["ema20"] > bl["ema50"])
    metrics = {"btc_risk_on": btc_risk_on}
    notes = [f"BTC regime: {'risk-on' if btc_risk_on else 'risk-off / cautious'} "
             f"(BTC {'above' if bl['close'] > btc_ema_long else 'below'} its long daily EMA)."]
    if is_btc:
        score = 75 if btc_risk_on else 35
        return _result("Market regime & relative strength", score, notes, metrics)

    j = d1[["close"]].join(b[["close"]], rsuffix="_btc", how="inner")
    r30 = j["close"].iloc[-1] / j["close"].iloc[-31] - 1
    b30 = j["close_btc"].iloc[-1] / j["close_btc"].iloc[-31] - 1
    n90 = min(91, len(j))
    r90 = j["close"].iloc[-1] / j["close"].iloc[-n90] - 1
    b90 = j["close_btc"].iloc[-1] / j["close_btc"].iloc[-n90] - 1
    corr = j.pct_change().tail(90).corr().iloc[0, 1]
    rs30, rs90 = r30 - b30, r90 - b90
    score = 50 + np.clip(rs30 * 150, -30, 30) + np.clip(rs90 * 60, -20, 20)
    score += 10 if btc_risk_on else -15
    notes.append(f"30d: coin {r30:+.1%} vs BTC {b30:+.1%} ({rs30:+.1%}); "
                 f"90d: {r90:+.1%} vs {b90:+.1%} ({rs90:+.1%}). Correlation to BTC {corr:.2f}.")
    if rs30 > 0 and rs90 > 0:
        notes.append("Outperforming BTC on both horizons — relative leader.")
    elif rs30 < 0 and rs90 < 0:
        notes.append("Underperforming BTC on both horizons — capital is flowing elsewhere.")
    metrics.update({"ret_30d_pct": round(r30 * 100, 2), "btc_ret_30d_pct": round(b30 * 100, 2),
                    "ret_90d_pct": round(r90 * 100, 2), "btc_ret_90d_pct": round(b90 * 100, 2),
                    "corr_btc_90d": round(float(corr), 2)})
    return _result("Market regime & relative strength", score, notes, metrics)


# --------------------------------------------------------------------- flow
def flow(m15: pd.DataFrame, h1: pd.DataFrame, d1: pd.DataFrame) -> dict:
    """Taker-side flow and trade intensity. 15m bars for the last 24h,
    1h for 7 days, daily for the 30-day baseline."""
    tk24 = m15["taker_buy_quote"].tail(96).sum() / m15["quote_volume"].tail(96).sum()
    tk7 = h1["taker_buy_quote"].tail(168).sum() / h1["quote_volume"].tail(168).sum()
    tk30 = d1["taker_buy_quote"].tail(30).sum() / d1["quote_volume"].tail(30).sum()
    intensity = m15["trades"].tail(96).mean() / m15["trades"].tail(672).mean()
    v7, v30 = d1["quote_volume"].tail(7).mean(), d1["quote_volume"].tail(30).mean()
    vr = v7 / v30 if v30 else 1
    price7 = d1["close"].iloc[-1] / d1["close"].iloc[-8] - 1
    s = 50 + np.clip((tk7 - 0.5) * 400, -20, 20) + np.clip((tk7 - tk30) * 600, -10, 10) \
        + np.clip((tk24 - 0.5) * 300, -10, 10)
    notes = [f"Taker-buy share 24h {tk24:.1%}, 7d {tk7:.1%}, 30d {tk30:.1%}; "
             f"24h trade intensity {intensity:.2f}x the 7-day norm; 7d volume {vr:.2f}x the 30d avg; "
             f"7d price change {price7:+.1%}."]
    if price7 > 0 and vr > 1.2:
        s += 10; notes.append("Price rising on expanding volume — demand confirmation.")
    elif price7 > 0 and vr < 0.8:
        s -= 5; notes.append("Price rising on fading volume — weak confirmation.")
    elif price7 < 0 and vr > 1.2:
        s -= 10; notes.append("Price falling on expanding volume — distribution.")
    if intensity > 2.5:
        notes.append("Unusual trade-count spike in the last 24h — news or a pump; expect volatility.")
    return _result("Order flow & volume", s, notes, {
        "taker_buy_share_24h": round(float(tk24), 4), "taker_buy_share_7d": round(float(tk7), 4),
        "taker_buy_share_30d": round(float(tk30), 4), "trade_intensity_24h": round(float(intensity), 2),
        "volume_7d_vs_30d": round(float(vr), 2), "price_7d_pct": round(float(price7) * 100, 2)})
