"""The whole pipeline offline: audit -> report -> logs -> review grading -> point-in-time replay."""
import csv
import json

import pytest

import audit as au
import binance_client as bc


@pytest.fixture
def isolated_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(au, "LOG", tmp_path / "logs" / "audits.csv")
    monkeypatch.setattr(au, "SIGNAL_LOG", tmp_path / "logs" / "signals.csv")
    monkeypatch.setattr(au, "TRACK_RECORD", tmp_path / "logs" / "track_record.json", raising=False)
    return tmp_path


@pytest.fixture(scope="session")
def demo_audit(universe_dir):
    client = bc.OfflineClient(universe_dir, as_of="2025-01-15T12:00:00Z")
    fut = bc.OfflineFuturesClient(universe_dir, as_of="2025-01-15T12:00:00Z")
    now = bc.pd.Timestamp("2025-01-15T12:00:00Z").to_pydatetime()
    return au.audit(client, fut, "DEMO", "USDT", "4h", use_ml=True, use_market=False, now=now,
                    strategies=["trend", "dip", "double_triple_bottom_v1", "range_support_v1"])


def test_audit_structure(demo_audit):
    a = demo_audit
    assert a["symbol"] == "DEMOUSDT" and a["timeframe"] == "4h" and a["point_in_time"] and a["offline"]
    assert a["verdict"] in {"FAVORABLE", "WATCHLIST", "NEUTRAL", "AVOID"} and 0 <= a["score"] <= 100
    assert a["data_until_utc"] == "2025-01-15 08:00"           # the 08:00-12:00 candle was still forming
    assert len(a["strategies"]) >= 10 and all(len(r["gates"]) == 12 for r in a["strategies"])
    assert all(0 <= r["fdr_q"] <= 1 for r in a["strategies"])
    assert all("pbo" in r["overfit"] and "dsr" in r["overfit"] for r in a["strategies"])
    names = [c["name"] for c in a["checks"]]
    assert names == list(au.WEIGHTS)
    assert a["plan"]["stop"] < a["plan"]["entry_low"] < a["plan"]["target1"] < a["plan"]["target2"]
    assert {x["name"] for x in a["research"]} >= {"Cointegrated pairs", "Funding / basis carry (delta-neutral)",
                                                  "Range-gated grid", "DCA accumulation (weekly)", "Funding / OI squeeze"}
    assert a["patterns"]["structure"] in {"bullish", "bearish", "mixed", "unknown"}
    for s in a["signals"]:
        assert set(au.SIGNAL_FIELDS) <= set(s)
        assert s["decision"] == "APPROVED" or s["decision"].startswith("BLOCKED")
        if s["decision"] != "APPROVED":
            assert s["max_risk_fraction"] == 0.0


def test_meta_labeler_ran_or_explained(demo_audit):
    m = demo_audit["meta"]
    assert "available" in m
    if m["available"]:
        assert 0 <= m["wf_auc"] <= 1 and "top_features" in m and "calibrated" in m


def test_markdown_and_save_and_logs(demo_audit, isolated_outputs):
    a = json.loads(json.dumps(demo_audit, default=au._json))   # a deep copy that survives save()'s pop
    a["_qs"] = demo_audit["_qs"]
    md = au.to_markdown(a)
    assert "## Strategy library" in md and "G10 PBO" in md and "## Trade plan" in md and "FDR q" in md
    path = au.save(a)
    assert path.exists() and path.with_suffix(".json").exists()
    rows = list(csv.DictReader(au.LOG.open()))
    assert len(rows) == 1 and rows[0]["symbol"] == "DEMOUSDT" and rows[0]["outcome"] == ""


def test_review_grades_past_audits(demo_audit, isolated_outputs, universe_dir):
    a = json.loads(json.dumps(demo_audit, default=au._json))
    a["_qs"] = demo_audit["_qs"]
    au.save(a)
    later = bc.OfflineClient(universe_dir)                    # the future is now known
    au.review(later)
    rows = list(csv.DictReader(au.LOG.open()))
    assert rows[0]["outcome"] in {"target1", "stop", "timeout"} and rows[0]["outcome_return_pct"] != ""


def test_strategy_filter_and_stale_kill_switch(universe_dir):
    client = bc.OfflineClient(universe_dir, as_of="2025-01-15T12:00:00Z")
    stale_now = bc.pd.Timestamp("2025-01-20T00:00:00Z").to_pydatetime()   # 4.5 days after the last candle
    a = au.audit(client, None, "DEMO", "USDT", "4h", use_ml=False, use_market=False, now=stale_now,
                 strategies=["trend_donchian_v1"])
    assert [r["id"] for r in a["strategies"]] == ["trend_donchian_v1"]
    assert any("STALE DATA" in f for f in a["flags"])
    assert all(s["decision"].startswith("BLOCKED") for s in a["signals"])


def test_main_demo_cli(isolated_outputs, monkeypatch, universe_dir):
    monkeypatch.setattr(au, "DEMO_DIR", universe_dir)
    rc = au.main(["--demo", "--no-ml", "--tf", "4h", "--strategies", "trend_ema_adx_v1,range_support_v1"])
    assert rc == 0
    assert list(au.REPORTS.glob("DEMOUSDT_4h_*.md"))
