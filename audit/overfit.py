"""Backtest-overfitting statistics (v6).

* cscv_pbo          Probability of Backtest Overfitting via Combinatorially Symmetric Cross-Validation
                    (Bailey, Borwein, López de Prado & Zhu, 2017): how often does the parameter set that
                    wins in-sample fall into the bottom half out-of-sample?
* deflated_sharpe   Deflated Sharpe Ratio (Bailey & López de Prado, 2014): the probability that the
                    true Sharpe exceeds zero after correcting for the number of trials, the sample
                    length, skew and kurtosis.
* min_track_record  Minimum track-record length needed for the observed Sharpe to be significant.
* benjamini_hochberg  False-discovery-rate control across the whole strategy library.

All inputs are per-bar (or per-trade) return arrays; nothing here looks at prices.
"""
from __future__ import annotations

from itertools import combinations

import numpy as np
from scipy.stats import norm

EULER_GAMMA = 0.5772156649015329


def sharpe(r: np.ndarray) -> float:
    r = np.asarray(r, float)
    if len(r) < 2:
        return 0.0
    s = r.std(ddof=1)
    return float(r.mean() / s) if s > 0 else 0.0


def cscv_pbo(M: np.ndarray, S: int = 8, min_rows_per_block: int = 8) -> dict | None:
    """M: (T x N) matrix of per-bar returns, one column per parameter set, over the SAME period.
    Returns {"pbo", "p_loss_oos", "degradation_slope", "combos", "trials"} or None when there is
    not enough data. A single column (no selection) has PBO 0 by construction."""
    M = np.asarray(M, float)
    if M.ndim != 2 or M.shape[0] < S * min_rows_per_block:
        return None
    T, N = M.shape
    if N < 2:
        return {"pbo": 0.0, "p_loss_oos": float(sharpe(M[:, 0]) < 0), "degradation_slope": 0.0, "combos": 0, "trials": N}
    S = S if S % 2 == 0 else S + 1
    blocks = np.array_split(np.arange(T), S)
    sums = np.array([M[b].sum(axis=0) for b in blocks])            # S x N
    sq = np.array([(M[b] ** 2).sum(axis=0) for b in blocks])
    cnt = np.array([len(b) for b in blocks], float)

    def perf(idx):
        n = cnt[idx].sum()
        mean = sums[idx].sum(axis=0) / n
        var = sq[idx].sum(axis=0) / n - mean ** 2
        sd = np.sqrt(np.maximum(var, 0.0))
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(sd > 0, mean / sd, 0.0)
        return out

    all_idx = set(range(S))
    lam, oos_best, is_best, degr = [], [], [], []
    for train in combinations(range(S), S // 2):
        test = sorted(all_idx - set(train))
        r_is, r_oos = perf(list(train)), perf(test)
        n_star = int(np.argmax(r_is))
        rank = float((r_oos < r_oos[n_star]).sum() + 0.5 * (r_oos == r_oos[n_star]).sum() - 0.5)
        omega = (rank + 1) / (N + 1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        lam.append(np.log(omega / (1 - omega)))
        oos_best.append(r_oos[n_star]); is_best.append(r_is[n_star])
        degr.append((r_is[n_star], r_oos[n_star]))
    lam = np.array(lam)
    d = np.array(degr)
    slope = float(np.polyfit(d[:, 0], d[:, 1], 1)[0]) if len(d) > 2 and d[:, 0].std() > 0 else 0.0
    return {"pbo": round(float((lam < 0).mean()), 3), "p_loss_oos": round(float((np.array(oos_best) < 0).mean()), 3),
            "degradation_slope": round(slope, 3), "combos": int(len(lam)), "trials": N}


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max SR] of n_trials independent trials with SR variance var_sr (the DSR benchmark SR0)."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    return float(np.sqrt(var_sr) * ((1 - EULER_GAMMA) * norm.ppf(1 - 1 / n_trials)
                                    + EULER_GAMMA * norm.ppf(1 - 1 / (n_trials * np.e))))


def probabilistic_sharpe(sr: float, sr_bench: float, T: int, skew: float, kurt: float) -> float:
    """PSR: P(true SR > sr_bench) given T observations, skew and (non-excess) kurtosis."""
    if T < 3:
        return 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        denom = 1e-9
    z = (sr - sr_bench) * np.sqrt(T - 1) / np.sqrt(denom)
    return float(norm.cdf(z))


def _moments(r: np.ndarray) -> tuple[float, float]:
    r = np.asarray(r, float)
    if len(r) < 4 or r.std() == 0:
        return 0.0, 3.0
    z = (r - r.mean()) / r.std()
    return float((z ** 3).mean()), float((z ** 4).mean())


def deflated_sharpe(returns: np.ndarray, trial_sharpes: list[float] | np.ndarray, bars_per_year: float | None = None) -> dict:
    """Deflated Sharpe of the SELECTED return series given the Sharpe ratios of every trial that was run
    (the parameter grid). Per-bar Sharpe in, probabilities out."""
    r = np.asarray(returns, float)
    T = len(r)
    sr = sharpe(r)
    trials = np.asarray(trial_sharpes, float)
    n = int(max(len(trials), 1))
    var_sr = float(trials.var(ddof=1)) if n > 1 else 0.0
    sr0 = expected_max_sharpe(n, var_sr)
    skew, kurt = _moments(r)
    dsr = probabilistic_sharpe(sr, sr0, T, skew, kurt)
    psr0 = probabilistic_sharpe(sr, 0.0, T, skew, kurt)
    out = {"sharpe_per_bar": round(sr, 5), "sr0_per_bar": round(sr0, 5), "trials": n, "obs": T,
           "skew": round(skew, 3), "kurtosis": round(kurt, 3), "psr": round(psr0, 3), "dsr": round(dsr, 3)}
    if bars_per_year:
        out["sharpe_annual"] = round(sr * np.sqrt(bars_per_year), 2)
        out["sr0_annual"] = round(sr0 * np.sqrt(bars_per_year), 2)
    mtrl = min_track_record(sr, sr0, skew, kurt)
    out["min_track_record_bars"] = None if mtrl is None else int(np.ceil(mtrl))
    if bars_per_year and mtrl is not None:
        out["min_track_record_years"] = round(mtrl / bars_per_year, 2)
    return out


def min_track_record(sr: float, sr_bench: float, skew: float, kurt: float, alpha: float = 0.95) -> float | None:
    """Observations needed before the Sharpe is significant at `alpha` vs sr_bench (None if never)."""
    if sr <= sr_bench:
        return None
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    return float(1 + max(denom, 1e-9) * (norm.ppf(alpha) / (sr - sr_bench)) ** 2)


def benjamini_hochberg(pvals: list[float] | np.ndarray) -> np.ndarray:
    """FDR-adjusted p-values (q-values), same order as the input."""
    p = np.asarray(pvals, float)
    m = len(p)
    if m == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * m / (np.arange(m) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.clip(q, 0, 1)
    return out
