"""Overfitting statistics: PBO/CSCV, deflated Sharpe, minimum track record, Benjamini-Hochberg."""
import numpy as np
import pytest

import overfit


def test_pbo_high_for_noise_low_for_real_edge():
    pbos, edge_pbos = [], []
    for seed in range(12):
        rng = np.random.default_rng(seed)
        noise = rng.normal(0, 0.01, size=(2000, 12))
        r = overfit.cscv_pbo(noise)
        assert r["combos"] == 70 and r["trials"] == 12
        pbos.append(r["pbo"])
        edge = noise.copy()
        edge[:, 3] += 0.004                                 # one parameter set has a genuine edge
        r2 = overfit.cscv_pbo(edge)
        edge_pbos.append(r2["pbo"])
        assert r2["p_loss_oos"] <= 0.1
    # no real edge: the in-sample winner lands below the OOS median about half the time (one draw is noisy)
    assert 0.3 <= np.mean(pbos) <= 0.7 and max(edge_pbos) <= 0.15 and np.mean(edge_pbos) < np.mean(pbos)
    noise = np.random.default_rng(0).normal(0, 0.01, size=(2000, 12))
    assert overfit.cscv_pbo(noise[:, :1])["pbo"] == 0.0     # a single trial cannot be selection-biased
    assert overfit.cscv_pbo(noise[:30]) is None             # too short to split


def test_deflated_sharpe_penalises_trials_and_rewards_real_edge():
    rng = np.random.default_rng(1)
    T = 1500
    trials = rng.normal(0, 0.01, size=(T, 20))
    srs = [overfit.sharpe(trials[:, k]) for k in range(20)]
    best = int(np.argmax(srs))
    d = overfit.deflated_sharpe(trials[:, best], srs, bars_per_year=2190)
    assert d["dsr"] < 0.9 and d["sr0_per_bar"] > 0            # the best of 20 noise trials is not significant
    assert d["min_track_record_bars"] is None or d["min_track_record_bars"] > T
    good = rng.normal(0.003, 0.01, T)
    d2 = overfit.deflated_sharpe(good, [overfit.sharpe(good)], bars_per_year=2190)
    assert d2["dsr"] > 0.99 and d2["trials"] == 1 and d2["sr0_per_bar"] == 0.0
    assert d2["min_track_record_bars"] < T
    assert d2["sharpe_annual"] == pytest.approx(overfit.sharpe(good) * np.sqrt(2190), rel=0.05)


def test_probabilistic_sharpe_monotone():
    assert overfit.probabilistic_sharpe(0.05, 0.0, 1000, 0.0, 3.0) > overfit.probabilistic_sharpe(0.02, 0.0, 1000, 0.0, 3.0)
    assert overfit.probabilistic_sharpe(0.05, 0.0, 1000, -1.0, 6.0) < overfit.probabilistic_sharpe(0.05, 0.0, 1000, 0.0, 3.0)
    assert overfit.probabilistic_sharpe(0.05, 0.0, 2, 0.0, 3.0) == 0.0


def test_benjamini_hochberg_known_values():
    q = overfit.benjamini_hochberg([0.01, 0.04, 0.03, 0.2])
    # ranks: 0.01 -> 0.04, 0.03 -> 0.06, 0.04 -> 0.0533, 0.2 -> 0.2; then monotone from the top
    assert q.tolist() == pytest.approx([0.04, 0.04 * 4 / 3, 0.04 * 4 / 3, 0.2])
    assert overfit.benjamini_hochberg([]).size == 0
    assert overfit.benjamini_hochberg([0.5]).tolist() == [0.5]
