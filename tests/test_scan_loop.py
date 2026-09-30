"""Scanner, watch loop and notifications through the CLI, offline."""
import json

import pytest

import audit as au


@pytest.fixture
def isolated(tmp_path, monkeypatch, universe_dir):
    monkeypatch.setattr(au, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(au, "LOG", tmp_path / "logs" / "audits.csv")
    monkeypatch.setattr(au, "SIGNAL_LOG", tmp_path / "logs" / "signals.csv")
    monkeypatch.setattr(au, "TRACK_RECORD", tmp_path / "logs" / "track_record.json")
    monkeypatch.setattr(au, "DEMO_DIR", universe_dir)
    return tmp_path


def test_scan_ranks_universe_and_writes_report(isolated):
    rc = au.main(["--demo", "--scan", "3", "--no-ml", "--tf", "4h", "--strategies", "trend_donchian_v1,range_support_v1",
                  "--jobs", "1", "--dashboard"])
    assert rc == 0
    md = list(au.REPORTS.glob("scan_4h_*.md"))
    js = list(au.REPORTS.glob("scan_4h_*.json"))
    assert len(md) == 1 and len(js) == 1
    data = json.loads(js[0].read_text())
    syms = [r["symbol"] for r in data["rows"]]
    assert set(syms) == {"BTCUSDT", "ETHUSDT", "DEMOUSDT"}
    assert len(data["correlation"]) == 3 and all("rs_percentile" in r for r in data["rows"])
    assert (au.REPORTS / "dashboard.html").exists()
    assert len(list(au.REPORTS.glob("*USDT_4h_*.md"))) == 3       # every coin got its own full report


def test_scan_parallel_workers(isolated):
    rc = au.main(["--demo", "--scan", "2", "--no-ml", "--tf", "4h", "--strategies", "trend_donchian_v1", "--jobs", "2"])
    assert rc == 0 and list(au.REPORTS.glob("scan_4h_*.md"))


def test_watch_loop_reruns_and_notifies(isolated, monkeypatch):
    sleeps = []
    monkeypatch.setattr(au.time, "sleep", lambda s: sleeps.append(s))
    sent = []
    monkeypatch.setattr(au.notify, "send", lambda text, **k: sent.append(text) or ["test"])
    monkeypatch.setattr(au.notify, "should_notify", lambda v, m=None: True)
    rc = au.main(["--demo", "--no-ml", "--tf", "4h", "--strategies", "trend_donchian_v1", "--loop", "--loop-max", "2", "--notify"])
    assert rc == 0
    assert len(sleeps) == 1 and 30 <= sleeps[0] <= 4 * 3600 + 30
    assert len(sent) == 2 and "DEMOUSDT 4h" in sent[0]
    assert len(list(au.REPORTS.glob("DEMOUSDT_4h_*.md"))) == 2 and (au.REPORTS / "dashboard.html").exists()


def test_next_close_seconds():
    assert au.next_close_seconds("1h", now_ts=3_600 * 10 + 100, grace=0) == pytest.approx(3_500)
    assert au.next_close_seconds("4h", now_ts=0, grace=30) == pytest.approx(4 * 3_600 + 30)
