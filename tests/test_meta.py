"""Meta-labeler building blocks: uniqueness weights, bet sizing, conformal decisions, drift."""
import numpy as np
import pytest

import meta_model as mm


def test_uniqueness_weights_penalise_overlap():
    # two fully overlapping labels + one isolated label
    d = np.array([10, 10, 50]); x = np.array([20, 20, 60])
    w = mm.uniqueness_weights(d, x, 100)
    assert w[0] == pytest.approx(w[1]) and w[2] > w[0] and w.mean() == pytest.approx(1.0)


def test_bet_size_and_multiplier():
    assert mm.bet_size(0.5) == pytest.approx(0.0, abs=1e-9)
    assert 0 < mm.bet_size(0.6) < mm.bet_size(0.75) < mm.bet_size(0.95) <= 1.0
    assert mm.size_multiplier(0.4) == 0.0 and mm.size_multiplier(0.55) == 0.25 and mm.size_multiplier(0.9) == 1.0


def test_conformal_decisions():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 400)
    p = np.clip(0.5 + 0.3 * (y - 0.5) + rng.normal(0, 0.15, 400), 0.01, 0.99)   # informative but noisy
    q = mm.conformal_qhat(p, y, alpha=0.2)
    assert 0 < q < 1
    # empirical coverage of the prediction sets on the calibration data is at least 1 - alpha
    covered = np.mean([(yy == 1 and pp >= 1 - q) or (yy == 0 and (1 - pp) >= 1 - q) for pp, yy in zip(p, y)])
    assert covered >= 0.79
    assert mm.conformal_decision(0.95, q) == "accept" and mm.conformal_decision(0.05, q) == "reject"
    assert mm.conformal_decision(0.5, q) == "abstain"


def test_psi_detects_shift():
    rng = np.random.default_rng(1)
    base = rng.normal(0, 1, 2000)
    assert mm.psi(base, rng.normal(0, 1, 300)) < 0.1
    assert mm.psi(base, rng.normal(2, 1, 300)) > 0.5
    assert mm.psi(base, base[:3]) == 0.0
