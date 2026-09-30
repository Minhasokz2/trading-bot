"""G0: every strategy's signal on a bar must be identical whether or not later bars exist.
A deliberately leaking strategy must FAIL the self-test."""
import itertools

import numpy as np
import pytest

import strategies as st
import validation as val

CUTS = lambda n: (n - 200, n - 77, n - 30, n - 9, n - 2)   # noqa: E731


@pytest.mark.parametrize("strategy", st.LIBRARY, ids=[s.id for s in st.LIBRARY])
def test_signals_do_not_change_with_future_bars(strategy, h4_frame, ctx4):
    keys = list(strategy.space)
    grid = [dict(zip(keys, v)) for v in itertools.product(*strategy.space.values())]
    st._CACHE.clear()
    for p in grid[:2]:
        assert val.lookahead_test(strategy, h4_frame, ctx4, p, cuts=CUTS(len(h4_frame))), (strategy.id, p)


class _Leaky(st.Strategy):
    """Uses tomorrow's close — the self-test must catch it."""
    id, name, family = "leaky_test", "leaky", "test"
    space = {"k": [1]}

    def signals(self, df, ctx, p):
        c = df["close"]
        e = (c.shift(-1) > c * 1.001).fillna(False).values     # future information
        return e, np.zeros(len(df), bool), None


def test_leaky_strategy_is_caught(h4_frame, ctx4):
    assert not val.lookahead_test(_Leaky(), h4_frame, ctx4, {"k": 1}, cuts=CUTS(len(h4_frame)))


def test_validate_returns_all_gates_and_rejects_leak(h4_frame, ctx4):
    res = val.validate(_Leaky(), h4_frame, ctx4)
    assert res.status == "REJECTED" and res.gates["G0_lookahead"] is False
    assert len(res.gates) == val.N_GATES
    res2 = val.validate(st.LIBRARY[0], h4_frame, ctx4)
    assert set(res2.gates) >= {"G10_pbo", "G11_deflated_sharpe"} and res2.overfit["trials"] == 9
    assert res2.status in {"ACCEPTED", "CANDIDATE", "REJECTED"}
