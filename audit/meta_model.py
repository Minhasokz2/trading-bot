"""ML meta-labeler (LightGBM). It never invents trades: it scores the signals the rule
strategies already produce and learns whether to ACCEPT / SIZE / REJECT them.

Events  = every historical entry of every validated strategy (its chosen parameters).
Target  = was that trade net-positive after fees + slippage (its own stop/exit/timeout)?
Features (known at the decision candle): strategy id, regime and volatility state, trend
strength, RSI/ADX, distance to EMAs, volume and taker flow, BTC state, time, and the
strategy's recent degradation (mean of its last 5 trades that had already CLOSED).

Validation: time-ordered walk-forward with an embargo (a training event must have exited
before the test fold starts) + an untouched final holdout scored once.

v6 additions (López de Prado, *Advances in Financial Machine Learning*, ch. 4, 7, 10; Vovk et al.):
  * sample weights by label uniqueness — overlapping trades no longer count as independent evidence
  * isotonic calibration of the out-of-fold probabilities, so 60% means 60%
  * bet sizing from the calibrated probability: size = 2·Φ(z) − 1, z = (p − ½)/√(p(1 − p))
  * split-conformal decision at 1 − α coverage: ACCEPT / ABSTAIN / REJECT instead of a bare threshold
  * feature-drift check (population stability index) — a drifted model is switched off, not trusted
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from scipy.stats import norm
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score, brier_score_loss

from regime import efficiency_ratio
from settings import CFG

warnings.filterwarnings("ignore")
FOLDS, HOLDOUT = 4, 0.15
MIN_EVENTS = CFG["meta"]["min_events"]
ALPHA = CFG["meta"]["conformal_alpha"]
PSI_WARN = CFG["meta"]["drift_psi_warn"]


def _feature_frame(df: pd.DataFrame, ctx: dict) -> pd.DataFrame:
    c = df["close"]
    f = pd.DataFrame(index=df.index)
    f["rsi"], f["adx"], f["atr_pct"], f["bbw"] = df["rsi"], df["adx"], df["atr_pct"], df["bbw"]
    for n in (20, 50, 200):
        f[f"dist_ema{n}"] = (c - df[f"ema{n}"]) / df["atr"]
    f["er"] = efficiency_ratio(c)
    f["vol_ratio"], f["taker_ratio"] = df["vol_ratio"], df["taker_ratio"]
    f["ret_10"], f["ret_30"] = c.pct_change(10), c.pct_change(30)
    f["vol_rank"] = df["atr_pct"].rolling(500, min_periods=100).rank(pct=True)
    b = ctx.get("btc_close")
    if b is not None:
        b = b.reindex(df.index)
        f["btc_ret_10"], f["btc_ret_30"] = b.pct_change(10), b.pct_change(30)
    f["btc_risk_on"] = ctx["btc_risk_on"].reindex(df.index) if ctx.get("btc_risk_on") is not None else 1
    f["hour"], f["dow"] = df.index.hour, df.index.dayofweek
    return f


def _model():
    return LGBMClassifier(n_estimators=250, learning_rate=0.03, num_leaves=15, max_depth=4,
                          min_child_samples=25, subsample=0.8, subsample_freq=1,
                          colsample_bytree=0.8, reg_lambda=1.0, random_state=7, verbose=-1)


def build_events(df, ctx, validated: list[tuple]) -> pd.DataFrame:
    """validated: [(strategy, Result), ...] for strategies that were not REJECTED on lookahead."""
    F = _feature_frame(df, ctx)
    rows = []
    for k, (s, res) in enumerate(validated):
        t = res.trades_dev
        if t is None or len(t.ret) == 0:
            continue
        for j in range(len(t.ret)):
            d = t.entry_i[j] - 1                      # decision candle
            closed = t.ret[:j][t.exit_i[:j] < d]      # trades already closed at decision time
            rows.append({"decision_i": d, "exit_i": t.exit_i[j], "strategy": k,
                         "recent_perf": float(closed[-5:].mean()) if len(closed) else 0.0,
                         "y": int(t.ret[j] > 0), "ret": float(t.ret[j])})
    if not rows:
        return pd.DataFrame()
    ev = pd.DataFrame(rows).sort_values("decision_i").reset_index(drop=True)
    X = F.iloc[ev["decision_i"].values].reset_index(drop=True)
    for k in range(len(validated)):
        X[f"strat_{k}"] = (ev["strategy"] == k).astype(int)
    return pd.concat([ev, X], axis=1)


# ---------------------------------------------------------------- v6 helpers
def uniqueness_weights(decision_i: np.ndarray, exit_i: np.ndarray, n_bars: int) -> np.ndarray:
    """Average uniqueness of each label: mean over its bars of 1 / (number of concurrent labels).
    Weights are rescaled to average 1 so the effective sample size is unchanged."""
    conc = np.zeros(n_bars + 1)
    for d, x in zip(decision_i, exit_i):
        conc[d:min(x, n_bars) + 1] += 1
    u = np.array([(1.0 / conc[d:min(x, n_bars) + 1]).mean() for d, x in zip(decision_i, exit_i)])
    return u / u.mean() if len(u) and u.mean() > 0 else np.ones(len(u))


def bet_size(p: float) -> float:
    """López de Prado bet size from a (calibrated) probability: 2·Φ(z) − 1, z = (p − ½)/√(p(1 − p))."""
    p = float(np.clip(p, 1e-6, 1 - 1e-6))
    z = (p - 0.5) / np.sqrt(p * (1 - p))
    return float(max(0.0, 2 * norm.cdf(z) - 1))


def size_multiplier(p: float) -> float:
    """Confidence-scaled fraction of the strategy's risk cap: full size around p ≈ 0.75, floor 0.25."""
    return float(np.clip(bet_size(p) / 0.5, 0.25, 1.0)) if p > 0.5 else 0.0


def conformal_qhat(p_cal: np.ndarray, y: np.ndarray, alpha: float = ALPHA) -> float:
    """Split-conformal threshold from calibration nonconformity scores s = 1 − p̂(true class)."""
    s = np.where(y == 1, 1 - p_cal, p_cal)
    n = len(s)
    k = int(np.ceil((n + 1) * (1 - alpha)))
    k = min(max(k, 1), n)
    return float(np.sort(s)[k - 1])


def conformal_decision(p: float, qhat: float) -> str:
    """Prediction set at coverage 1 − α: {win} -> accept, {loss} -> reject, both/empty -> abstain."""
    win_in, loss_in = p >= 1 - qhat, (1 - p) >= 1 - qhat
    if win_in and not loss_in:
        return "accept"
    if loss_in and not win_in:
        return "reject"
    return "abstain"


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    """Population stability index between a reference and a recent sample of one feature."""
    e = np.asarray(expected, float); a = np.asarray(actual, float)
    e, a = e[np.isfinite(e)], a[np.isfinite(a)]
    if len(e) < 20 or len(a) < 5:
        return 0.0
    edges = np.unique(np.quantile(e, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    eh = np.histogram(e, edges)[0] / len(e)
    ah = np.histogram(a, edges)[0] / len(a)
    eh, ah = np.clip(eh, 1e-4, None), np.clip(ah, 1e-4, None)
    return float(((ah - eh) * np.log(ah / eh)).sum())


def drift_report(X_train: pd.DataFrame, F_recent: pd.DataFrame, features: list[str], top: list[str]) -> dict:
    worst, worst_f = 0.0, None
    for f in top:
        if f not in features or f.startswith("strat_") or f not in F_recent.columns:
            continue
        v = psi(X_train[f].values, F_recent[f].values)
        if v > worst:
            worst, worst_f = v, f
    return {"max_psi": round(worst, 3), "feature": worst_f, "warning": bool(worst > PSI_WARN),
            "threshold": PSI_WARN}


# -------------------------------------------------------------------- run
def run(df, ctx, validated: list[tuple], active: list[int]) -> dict:
    ev = build_events(df, ctx, validated)
    if len(ev) < MIN_EVENTS:
        return {"available": False, "reason": f"only {len(ev)} historical strategy signals (need {MIN_EVENTS}+)"}
    feat = [c for c in ev.columns if c not in ("decision_i", "exit_i", "strategy", "y", "ret")]
    X, y, d, x_i = ev[feat], ev["y"].values, ev["decision_i"].values, ev["exit_i"].values
    w = uniqueness_weights(d, x_i, len(df))
    n = len(ev)
    h0 = int(n * (1 - HOLDOUT))
    start = int(h0 * 0.4)
    step = (h0 - start) // FOLDS
    aucs, briers, bases, lifts = [], [], [], []
    oof_p, oof_y = [], []
    for f in range(FOLDS):
        lo_, hi_ = start + f * step, start + (f + 1) * step
        tr = np.where(x_i[:lo_] < d[lo_])[0]          # embargo: trade must have exited
        if len(tr) < 50 or len(np.unique(y[tr])) < 2 or len(np.unique(y[lo_:hi_])) < 2:
            continue
        m = _model().fit(X.iloc[tr], y[tr], sample_weight=w[tr])
        p = m.predict_proba(X.iloc[lo_:hi_])[:, 1]
        yt, rt = y[lo_:hi_], ev["ret"].values[lo_:hi_]
        aucs.append(roc_auc_score(yt, p)); briers.append(brier_score_loss(yt, p)); bases.append(yt.mean())
        acc = p >= np.median(p)
        lifts.append(rt[acc].mean() - rt.mean())      # return improvement from filtering
        oof_p.append(p); oof_y.append(yt)
    if not aucs:
        return {"available": False, "reason": "not enough labelled folds"}
    oof_p, oof_y = np.concatenate(oof_p), np.concatenate(oof_y)

    # calibration + conformal threshold from out-of-fold predictions only
    calibrated = len(oof_p) >= 50 and len(np.unique(oof_y)) == 2
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99).fit(oof_p, oof_y) if calibrated else None
    cal = (lambda p: iso.predict(np.asarray(p, float))) if calibrated else (lambda p: np.asarray(p, float))
    p_cal_oof = cal(oof_p)
    qhat = conformal_qhat(p_cal_oof, oof_y, ALPHA)

    tr = np.where(x_i[:h0] < d[h0])[0]
    m = _model().fit(X.iloc[tr], y[tr], sample_weight=w[tr])
    ph = m.predict_proba(X.iloc[h0:])[:, 1]
    yh, rh = y[h0:], ev["ret"].values[h0:]
    h_auc = roc_auc_score(yh, ph) if len(np.unique(yh)) == 2 else float("nan")
    h_lift = float(rh[ph >= np.median(ph)].mean() - rh.mean()) if len(rh) else 0.0
    wf_auc, wf_lift = float(np.mean(aucs)), float(np.mean(lifts))
    has_edge = wf_auc >= 0.55 and wf_lift > 0 and h_lift > 0 and (np.isnan(h_auc) or h_auc >= 0.52)

    final = _model().fit(X, y, sample_weight=w)
    imp = pd.Series(final.feature_importances_, index=feat).sort_values(ascending=False)
    top = list(imp.head(5).index)
    Fall = _feature_frame(df, ctx)
    drift = drift_report(X, Fall.tail(50), feat, top)
    edge_reason = ""
    if has_edge and drift["warning"]:
        has_edge, edge_reason = False, f"feature drift: PSI {drift['max_psi']} on {drift['feature']} (> {PSI_WARN})"

    Fnow = Fall.iloc[[-1]].reset_index(drop=True)
    med = X.median(numeric_only=True)
    probs, active_out = {}, {}
    for k in active:
        row = Fnow.copy()
        for j in range(len(validated)):
            row[f"strat_{j}"] = int(j == k)
        t = validated[k][1].trades_dev
        row["recent_perf"] = float(t.ret[-5:].mean()) if t is not None and len(t.ret) else 0.0
        p_raw = float(final.predict_proba(row[feat].fillna(med))[:, 1][0])
        p = float(cal([p_raw])[0])
        sid = validated[k][0].id
        probs[sid] = round(p, 3)
        active_out[sid] = {"p_raw": round(p_raw, 3), "p": round(p, 3), "bet_size": round(bet_size(p), 3),
                           "size_multiplier": round(size_multiplier(p), 3),
                           "decision": conformal_decision(p, qhat)}
    return {"available": True, "events": n, "wf_auc": round(wf_auc, 3), "wf_brier": round(float(np.mean(briers)), 4),
            "wf_base_rate": round(float(np.mean(bases)), 3), "wf_filter_lift_pct": round(wf_lift * 100, 3),
            "holdout_auc": None if np.isnan(h_auc) else round(float(h_auc), 3),
            "holdout_filter_lift_pct": round(h_lift * 100, 3), "has_edge": bool(has_edge), "edge_reason": edge_reason,
            "accept_threshold": 0.5, "calibrated": bool(calibrated),
            "calibration_brier": round(float(brier_score_loss(oof_y, p_cal_oof)), 4),
            "conformal_alpha": ALPHA, "conformal_qhat": round(qhat, 3),
            "uniqueness_mean": round(float(np.mean(w)), 3), "uniqueness_min": round(float(np.min(w)), 3),
            "drift": drift, "active_probs": probs, "active": active_out, "top_features": top}
