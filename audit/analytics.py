"""Performance analytics via quantstats (ranaroussi/quantstats):
Sharpe, Sortino, Calmar, worst rolling period, and an HTML tearsheet
comparing the baseline strategy with buy & hold."""
from __future__ import annotations

import logging
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")
logging.getLogger("matplotlib").setLevel(logging.ERROR)
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

import quantstats as qs  # noqa: E402


def _daily(r: pd.Series) -> pd.Series:
    r = (1 + r).resample("1D").prod() - 1
    r.index = r.index.tz_localize(None)
    return r


def metrics(returns: pd.Series, benchmark: pd.Series) -> dict:
    r, b = _daily(returns), _daily(benchmark)
    p = 365
    worst_30 = float(((1 + r).rolling(30).apply(lambda x: x.prod(), raw=True) - 1).min())
    return {"sharpe": round(float(qs.stats.sharpe(r, periods=p)), 2),
            "sortino": round(float(qs.stats.sortino(r, periods=p)), 2),
            "calmar": round(float(qs.stats.calmar(r)), 2),
            "max_dd_pct": round(float(qs.stats.max_drawdown(r)) * 100, 1),
            "worst_30d_pct": round(worst_30 * 100, 1),
            "exposure_pct": round(float((returns != 0).mean()) * 100, 1),
            "bh_sharpe": round(float(qs.stats.sharpe(b, periods=p)), 2),
            "bh_max_dd_pct": round(float(qs.stats.max_drawdown(b)) * 100, 1)}


def tearsheet(returns: pd.Series, benchmark: pd.Series, path: Path, title: str) -> Path | None:
    try:
        qs.reports.html(_daily(returns), benchmark=_daily(benchmark), output=str(path),
                        title=title, periods_per_year=365, download_filename=path.name)
        return path
    except Exception as e:  # tearsheet is a nice-to-have; never fail the audit on it
        print(f"  (tearsheet skipped: {e})")
        return None
