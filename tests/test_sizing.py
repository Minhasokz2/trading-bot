import numpy as np
import pytest

import sizing


def test_kelly_scales_with_edge_and_is_conservative():
    rng = np.random.default_rng(0)
    good = rng.normal(0.02, 0.05, 300)               # strong edge -> full Kelly hits the no-leverage cap
    k = sizing.kelly(good)
    assert k["full_kelly"] > 1.0 and k["kelly_conservative"] <= k["full_kelly"]
    assert 0 < k["suggested_capital_fraction"] <= 1.0
    thin = rng.normal(0.002, 0.05, 300)              # thin edge -> small stake
    k2 = sizing.kelly(thin)
    assert k2["suggested_capital_fraction"] < k["suggested_capital_fraction"]
    bad = rng.normal(-0.01, 0.05, 300)
    assert sizing.kelly(bad)["suggested_capital_fraction"] == 0.0
    assert sizing.kelly([0.01] * 5)["suggested_capital_fraction"] == 0.0     # too few trades
    assert sizing.risk_fraction(0.5, 0.03, cap=0.003) == 0.003 and sizing.risk_fraction(0.05, 0.03, cap=0.003) == pytest.approx(0.0015)


def test_impact_cost_walks_the_book():
    c = sizing.impact_cost(order_usd=10_000, side_depth_usd=100_000, spread_bps=4.0)
    assert c["slippage_bps"] == pytest.approx(2.0 + 5.0) and not c["exceeds_1pct_depth"]
    big = sizing.impact_cost(order_usd=250_000, side_depth_usd=100_000, spread_bps=4.0)
    assert big["exceeds_1pct_depth"] and big["slippage_bps"] > 100
    assert sizing.max_order_for_slippage(100_000, 4.0, 5.0) == pytest.approx(6_000)
    assert sizing.max_order_for_slippage(100_000, 20.0, 5.0) == 0.0


def test_size_plan_respects_spot_and_flags_slippage():
    p = sizing.size_plan(account_usd=10_000, risk_frac=0.003, stop_pct=0.03, price=2.0, side_depth_usd=50_000,
                         spread_bps=5.0, assumed_slippage=0.0005, fee=0.001)
    assert p["position_usd"] == pytest.approx(1000.0) and p["units"] == pytest.approx(500.0)
    assert p["backtest_slippage_holds"] and p["round_trip_cost_pct"] > 0.2
    p2 = sizing.size_plan(account_usd=1_000_000, risk_frac=0.01, stop_pct=0.01, price=2.0, side_depth_usd=50_000,
                          spread_bps=5.0, assumed_slippage=0.0005, fee=0.001)
    assert p2["position_usd"] == 1_000_000 and not p2["backtest_slippage_holds"] and p2["exceeds_1pct_depth"]


def test_vol_target_and_portfolio_heat():
    assert sizing.vol_target(0.005, 0.02, 6) == pytest.approx(1.0)
    assert 0.1 <= sizing.vol_target(0.05, 0.02, 6) < 0.5
    cands = [{"symbol": "A", "risk_fraction": 0.006}, {"symbol": "B", "risk_fraction": 0.006},
             {"symbol": "C", "risk_fraction": 0.006}, {"symbol": "D", "risk_fraction": 0.0}]
    clusters = {"A": 0, "B": 0, "C": 1}
    out = sizing.portfolio_heat(cands, clusters, cap=0.015, max_positions=5, max_per_cluster=1)
    assert [o["allocated"] for o in out] == [True, False, True, False]
    assert out[1]["reason"] == "cluster cap" and out[3]["reason"] == "no risk budget"
    out2 = sizing.portfolio_heat(cands[:3], {}, cap=0.010, max_positions=5, max_per_cluster=5)
    assert sum(o["allocated_risk"] for o in out2) == pytest.approx(0.010)
    labels = sizing.clusters_from_correlation(np.array([[1, 0.9, 0.1], [0.9, 1, 0.2], [0.1, 0.2, 1]]), ["A", "B", "C"], 0.7)
    assert labels["A"] == labels["B"] != labels["C"]
