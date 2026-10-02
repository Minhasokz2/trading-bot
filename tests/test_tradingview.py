"""The TradingView indicator and its master prompt.

There is no Pine compiler outside TradingView, so the script is checked with tradingview/lint_pine.py (built-in names
and arguments against the Pine v6 reference, layout rules, the v6 lazy-evaluation rule) and cross-checked against the
Python bot it ports: the strategy order, regimes, exit mechanics, weights and verdict thresholds must not drift apart."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import market
import strategies as st
from settings import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
TV = ROOT / "tradingview"
PINE = TV / "coin_audit_library.pine"
PROMPT = TV / "INDICATOR_PROMPT.md"


def _lint():
    spec = importlib.util.spec_from_file_location("lint_pine", TV / "lint_pine.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pine() -> str:
    return PINE.read_text(encoding="utf-8")


def _array(name: str) -> list[str]:
    """The literal items of `var <type>[] name = array.from(...)` in the indicator."""
    m = re.search(r"var \w+\[\]\s+" + name + r"\s*=\s*array\.from\(", _pine())
    assert m, name
    i, depth = m.end(), 1
    while depth:
        depth += {"(": 1, ")": -1}.get(_pine()[i], 0)
        i += 1
    body = re.sub(r"//[^\n]*", "", _pine()[m.end():i - 1])
    return [x.strip().strip('"') for x in body.replace("\n", " ").split(",")]


def _defaults(s) -> dict:
    return {k: v[0] for k, v in s.space.items()}


# ------------------------------------------------------------------ the linter
def test_indicator_passes_the_offline_checks(capsys):
    assert _lint().lint(str(PINE), fast=True) == 0, capsys.readouterr().out


BAD = '''//@version=6
indicator("t", overlay = true, max_label_count = 10)
a = ta.exponential(close, 10)
b = math.maximum(1, 2)
label.new(bar_index, high, "x", colour = color.red, style = label.style_lable_up)
c = color.lightblue
d = close > open and
    high > low
arr = array.from(1.0, na)
if close > open
    e = ta.sma(close, 5)
x := 1
s = close > open and ta.sma(close, 20) > 0
f_hist(float v) => v - v[1]
g = close > 0 ? f_hist(close) : 0.0
'''


def test_linter_catches_planted_mistakes():
    lint = _lint()
    lay_e, _ = lint.layout_checks(BAD)
    sc_e, sc_w = lint.scope_checks(BAD)
    nm_e, _ = lint.fast_name_checks(BAD)
    errors = "\n".join(lay_e + sc_e + nm_e)
    for needle in ("max_label_count", "ta.exponential()", "math.maximum()", "'colour'", "label.style_lable_up",
                   "color.lightblue", "indented by 4 spaces", "bare `na`", "':=' on 'x'",
                   "ta.sma() after a lazy", "f_hist() after a lazy"):
        assert needle in errors, needle
    assert any("ta.sma() inside a local block" in w for w in sc_w)


# ------------------------------------------------------------ indicator vs bot
def test_indicator_is_read_only_and_has_the_27_strategies():
    code = _pine()
    assert code.startswith("//@version=6")
    assert re.search(r"^indicator\(", code, re.M)
    assert not re.search(r"^strategy\(|\bstrategy\.(entry|exit|order|close)\(", code, re.M)
    assert len(re.findall(r"^s\d\d = input\.string\(", code, re.M)) == 27
    assert len(st.LIBRARY) == 27


def test_strategy_regimes_and_chop_families_match_the_bot():
    assert _array("sReq") == [s.regime_required for s in st.LIBRARY]
    assert _array("sChopOk") == [str(s.family in market.RANGE_FAMILIES).lower() for s in st.LIBRARY]


def test_exit_mechanics_match_the_bot():
    x_atr = [float(v) for v in _array("xAtr")]
    x_max = [int(v) for v in _array("xMax")]
    tr_a = [float(v) for v in _array("xTrA")]
    tr_p = [float(v) for v in _array("xTrP")]
    lim = [int(v) for v in _array("xLimB")]
    for i, s in enumerate(st.LIBRARY):
        kw = s.exec_kw(_defaults(s))
        assert x_max[i] == (kw.get("max_bars") or 0), s.id
        assert (tr_a[i], tr_p[i]) == tuple(kw.get("trail") or (0.0, 0.0)), s.id
        assert lim[i] == kw.get("limit_bars", 0), s.id
        grid = s.space.get("stop_atr")
        if grid:
            assert x_atr[i] in grid, s.id                       # one of the values the bot tests
        else:
            assert x_atr[i] == kw.get("stop_atr", s.stop_atr), s.id


def test_weights_and_verdict_thresholds_match_the_settings():
    code = _pine()
    w = DEFAULTS["weights"]
    m = re.search(r"scoreRaw = \(10 \* sLiq \+ 13 \* sTrend \+ 6 \* sMom \+ 5 \* sVol \+ 9 \* sRs \+ 6 \* sFlow \+ wMkt \* "
                  r"nz\(sMkt\) \+ 28 \* sLib\)", code)
    assert m, "score formula changed — update this test and the prompt together"
    assert (w["Liquidity"], w["Trend (multi-timeframe)"], w["Momentum"], w["Volatility & risk"],
            w["Market regime & relative strength"], w["Order flow & volume"], w["Strategy library (validated)"]) \
        == (10, 13, 6, 5, 9, 6, 28)
    assert re.search(r"wMkt\s*=\s*cMAvail \? 15\.0", code) and w["Crypto-wide & macro regime"] == 15
    v = DEFAULTS["verdict"]
    assert f'score >= {v["favorable"]} ? "FAVORABLE" : score >= {v["watchlist"]} ? "WATCHLIST" : ' \
           f'score >= {v["neutral"]} ? "NEUTRAL"' in code
    g = DEFAULTS["gates"]
    assert re.search(r'gMinTr\s*= input\.int\(' + str(g["min_trades"]) + r",", code)
    assert re.search(r'gMinPf\s*= input\.float\(' + str(g["min_profit_factor"]) + r",", code)


# ------------------------------------------------------------------ the prompt
def test_prompt_documents_every_strategy_and_section():
    text = PROMPT.read_text(encoding="utf-8")
    assert "`PROMPT START`" in text and "`PROMPT END`" in text
    for k in range(1, 28):
        assert f"**{k:02d} · " in text, k
    for heading in ("## 2. Hard rules", "## 6. The market layer", "## 7. Pivot engine", "## 8. The 27 strategies",
                    "## 9. Bearish patterns", "## 10. Validation on the chart", "## 11. The approval chain",
                    "## 12. Checks, score and verdict", "## 13. The trade plan", "## 14. What to do now",
                    "## 15. User interface", "## 17. Acceptance tests", "## 18. Known differences"):
        assert heading in text, heading
    codes = _array("sCode")
    for code in codes:                                           # the strategy table names every dashboard code
        assert f"| {code} |" in text, code
