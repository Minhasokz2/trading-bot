"""Point-in-time replay of the whole audit over several dates, graded with the future (offline)."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
import verdict_backtest as vb  # noqa: E402


def test_replay_two_dates_and_grade(universe_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(vb, "RESULTS", tmp_path)
    rc = vb.main(["DEMO", "--tf", "4h", "--offline", str(universe_dir), "--no-ml", "--span", "40", "--every", "20",
                  "--strategies", "trend_ema_adx_v1,trend_donchian_v1", "--jobs", "1"])
    assert rc == 0
    out = tmp_path / "verdict_backtest_DEMOUSDT_4h.csv"
    df = pd.read_csv(out)
    assert len(df) == 2 and set(df["verdict"]) <= {"FAVORABLE", "WATCHLIST", "NEUTRAL", "AVOID"}
    assert df["outcome"].isin(["target1", "stop", "timeout"]).all()
    assert df["return_pct"].notna().all() and df["buy_hold_pct"].notna().all()
    assert (pd.to_datetime(df["as_of"]).diff().dropna().dt.days == 20).all()
    assert "Outcome by verdict" in vb.summarize(df)
