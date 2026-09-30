"""Regime classifier: trend/range, volatility state, BTC risk-on/off, liquidity, funding.

Higher-timeframe values are aligned by CLOSE time (a 1d bar is only known after it
closes), so no future information leaks onto lower-timeframe candles."""
from __future__ import annotations

import numpy as np
import pandas as pd

TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
HIGHER_TF = {"15m": "1h", "1h": "4h", "4h": "1d", "1d": "1w"}
BARS_PER_YEAR = {k: int(525600 / v) for k, v in TF_MINUTES.items()}


def align(base_index: pd.DatetimeIndex, base_tf: str, other: pd.DataFrame, other_tf: str,
          cols: list[str]) -> pd.DataFrame:
    """Values of `other` (a different timeframe) as known at each base candle's CLOSE.
    A higher-timeframe bar counts as known only once it has closed."""
    o = other[cols].sort_index()
    o_close = o.index + pd.Timedelta(minutes=TF_MINUTES[other_tf])
    b_close = base_index + pd.Timedelta(minutes=TF_MINUTES[base_tf])
    pos = o_close.searchsorted(b_close, side="right") - 1
    vals = np.full((len(base_index), len(cols)), np.nan)
    ok = pos >= 0
    vals[ok] = o.values[pos[ok]]
    return pd.DataFrame(vals, index=base_index, columns=cols)


def efficiency_ratio(close: pd.Series, n: int = 20) -> pd.Series:
    """Kaufman efficiency: |net move| / path length. ~1 = clean trend, ~0 = chop."""
    return (close.diff(n).abs() / close.diff().abs().rolling(n).sum()).fillna(0)


def classify(df: pd.DataFrame, tf: str, btc_risk_on: bool, liquidity_ok: bool,
             funding: dict | None, use_markov: bool = True) -> dict:
    last = df.iloc[-1]
    er = float(efficiency_ratio(df["close"]).iloc[-1])
    adx = float(last["adx"])
    trend = "trend" if (adx >= 22 or er >= 0.35) else "range" if (adx < 18 and er < 0.25) else "transition"
    direction = "up" if last["close"] > last["ema50"] else "down"
    yr = min(len(df), BARS_PER_YEAR[tf])
    vol_rank = float((df["atr_pct"].tail(yr) < last["atr_pct"]).mean())
    vol = "high" if vol_rank >= 0.8 else "low" if vol_rank <= 0.2 else "normal"
    fstate = "n/a"
    if funding and funding.get("available"):
        f = funding["mean_rate_8h"]
        fstate = "crowded-long" if f > 0.0003 else "negative" if f < 0 else "neutral"
    mv = markov_vol_regime(np.log(df["close"]).diff()) if use_markov else {"available": False}
    markov_high = bool(mv.get("available") and mv["p_high_vol"] is not None and mv["p_high_vol"] >= 0.8)
    return {"trend_state": trend, "direction": direction, "efficiency_ratio": round(er, 2),
            "adx": round(adx, 1), "vol_state": vol, "vol_percentile_1y": round(vol_rank, 2),
            "btc": "risk-on" if btc_risk_on else "risk-off", "liquidity": "ok" if liquidity_ok else "thin",
            "funding": fstate, "markov": mv, "markov_high_vol": markov_high}


def high_vol(reg: dict) -> bool:
    """High volatility by the trailing-year ATR percentile OR the Markov-switching filter (v6)."""
    return reg["vol_state"] == "high" or bool(reg.get("markov_high_vol"))


def matches(required: str, reg: dict) -> bool:
    if reg["liquidity"] != "ok":
        return False
    if required == "trend":
        return reg["trend_state"] in ("trend", "transition") and reg["direction"] == "up" and reg["btc"] == "risk-on"
    if required in ("trend_pullback", "momentum", "dip"):
        return reg["btc"] == "risk-on" and not high_vol(reg)
    if required == "range":
        return reg["trend_state"] == "range" and not high_vol(reg)
    return True


def align_events(base_index: pd.DatetimeIndex, base_tf: str, times: pd.DatetimeIndex,
                 values: np.ndarray) -> pd.Series:
    """Event data (e.g. funding settlements) as known at each base candle's close."""
    order = np.argsort(times.values)
    t, v = times[order], np.asarray(values)[order]
    b_close = base_index + pd.Timedelta(minutes=TF_MINUTES[base_tf])
    pos = t.searchsorted(b_close, side="right") - 1
    out = np.full(len(base_index), np.nan)
    ok = pos >= 0
    out[ok] = v[pos[ok]]
    return pd.Series(out, index=base_index)


# ------------------------------------------------------------ v6: Markov-switching volatility + HAR-RV
def markov_vol_regime(returns: pd.Series, max_obs: int = 1500) -> dict:
    """Two-state Markov-switching model of returns with regime-dependent variance (Hamilton, 1989),
    fitted with statsmodels. Returns the FILTERED probability (uses data up to the last bar only) that
    the market is in the high-variance state, plus each state's annualised-free volatility ratio.
    Used only as a live filter (never as a backtest feature)."""
    out = {"available": False, "p_high_vol": None, "high_state_vol_ratio": None, "expected_duration_bars": None}
    r = pd.Series(returns).dropna().tail(max_obs)
    if len(r) < 200 or r.std() == 0:
        out["reason"] = "not enough returns"
        return out
    try:
        import warnings
        from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            x = (r.values - r.values.mean()) / r.values.std() * 1.0
            mod = MarkovRegression(x, k_regimes=2, trend="c", switching_variance=True)
            res = mod.fit(disp=False, maxiter=200, em_iter=20)
        sig2 = np.asarray(res.params[-2:], float)               # last two params are the state variances
        hi = int(np.argmax(sig2))
        fp = np.asarray(res.filtered_marginal_probabilities)     # shape (nobs, k_regimes)
        sp = np.asarray(res.smoothed_marginal_probabilities)
        p_hi = float(fp[-1, hi])
        dur = res.expected_durations
        out.update({"available": True, "p_high_vol": round(p_hi, 3),
                    "high_state_vol_ratio": round(float(np.sqrt(sig2.max() / max(sig2.min(), 1e-12))), 2),
                    "expected_duration_bars": round(float(dur[hi]), 1),
                    "smoothed_share_high": round(float(sp[:, hi].mean()), 3),
                    "p_high_vol_5_bars_ago": round(float(fp[-6, hi]), 3) if len(fp) > 6 else None})
    except Exception as e:                                       # optimisation can fail on odd data
        out["reason"] = f"fit failed: {str(e)[:80]}"
    return out


def har_rv_forecast(intraday_close: pd.Series, bars_per_day: int) -> dict:
    """HAR-RV (Corsi, 2009): tomorrow's realised variance regressed on today's, the 5-day and the
    22-day average realised variance. Fitted by OLS on daily realised variance built from intraday
    returns; returns the next-day volatility forecast (in %) and the fit's R²."""
    r = np.log(intraday_close).diff().dropna()
    rv = (r ** 2).groupby(r.index.floor("D")).sum()
    rv = rv[rv > 0]
    if len(rv) < 60:
        return {"available": False, "reason": "need 60 days of intraday data"}
    d = pd.DataFrame({"rv": rv})
    d["rv5"], d["rv22"] = d["rv"].rolling(5).mean(), d["rv"].rolling(22).mean()
    d["y"] = d["rv"].shift(-1)
    fit = d.dropna()
    X = np.column_stack([np.ones(len(fit)), fit["rv"], fit["rv5"], fit["rv22"]])
    beta, *_ = np.linalg.lstsq(X, fit["y"].values, rcond=None)
    pred = X @ beta
    ss = ((fit["y"] - fit["y"].mean()) ** 2).sum()
    r2 = float(1 - ((fit["y"] - pred) ** 2).sum() / ss) if ss > 0 else 0.0
    last = d.iloc[-1]
    f = float(max(beta[0] + beta[1] * last["rv"] + beta[2] * last["rv5"] + beta[3] * last["rv22"], 1e-12))
    return {"available": True, "forecast_daily_vol_pct": round(np.sqrt(f) * 100, 2),
            "today_rv_vol_pct": round(np.sqrt(float(last["rv"])) * 100, 2),
            "avg22_vol_pct": round(np.sqrt(float(last["rv22"])) * 100, 2),
            "annualised_forecast_pct": round(np.sqrt(f * 365) * 100, 1), "r2": round(r2, 3),
            "betas": [round(float(b), 4) for b in beta], "days": int(len(fit))}
