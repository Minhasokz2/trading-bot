"""Job runner, retention and scheduler for the hosted app (no real audits: a fake audit script stands in)."""
import json
import os
import time

import pytest

import jobs


def wait_for(mgr, jid, states=jobs.TERMINAL, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = mgr.get(jid)
        if j["status"] in states:
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {jid} still {mgr.get(jid)['status']} after {timeout}s")


@pytest.fixture
def mgr(tmp_path, fake_audit):
    m = jobs.JobManager(tmp_path, fake_audit, poll=0.05, timeouts={"audit": 2})
    m.start()
    yield m
    m.stop()


# ------------------------------------------------------------------ arguments
def test_build_args_accepts_valid_requests():
    args, label = jobs.build_args("audit", {"coins": "sol, arb  inj", "tf": "1h", "ml": "on", "strategies": "trend, dip"})
    assert args[:5] == ["SOL", "ARB", "INJ", "--tf", "1h"] and "--dashboard" in args and "--no-ml" not in args
    assert args[args.index("--strategies") + 1] == "trend, dip" and "--no-tearsheet" in args and label == "Audit SOL ARB INJ · 1h"
    scan, _ = jobs.build_args("scan", {"n": "30", "tf": "4h", "jobs": 2, "ml": False})
    assert scan[:6] == ["--scan", "30", "--tf", "4h", "--jobs", "2"] and "--no-ml" in scan
    assert jobs.build_args("review", {})[0] == ["--review", "--dashboard"]
    demo, _ = jobs.build_args("demo", {})
    assert "--demo" in demo and "--no-ml" in demo


@pytest.mark.parametrize("kind, params", [
    ("audit", {"coins": ""}), ("audit", {"coins": "SOL;rm -rf /"}), ("audit", {"coins": "--review"}),
    ("audit", {"coins": "SOL", "tf": "2h"}), ("audit", {"coins": "A B C D E F G H I J K"}),
    ("audit", {"coins": "SOL", "strategies": "x; y"}), ("scan", {"n": 1}), ("scan", {"n": 500}), ("scan", {"n": "abc"}),
    ("scan", {"n": 20, "tf": "all"}), ("scan", {"n": 20, "jobs": 9}), ("nonsense", {})])
def test_build_args_rejects_bad_input(kind, params):
    with pytest.raises(jobs.JobError):
        jobs.build_args(kind, params)


# ------------------------------------------------------------------- manager
def test_job_runs_to_completion_and_collects_reports(mgr, tmp_path):
    job = mgr.submit("audit", {"coins": "SOL", "tf": "4h"})
    assert job["status"] == "queued" and jobs.JOB_ID_RE.match(job["id"])
    done = wait_for(mgr, job["id"])
    assert done["status"] == "done" and done["returncode"] == 0 and done["reports"] == ["SOLUSDT_4h_2026-01-01_0000.md"]
    log = mgr.log_tail(job["id"])
    assert "fake audit SOL --tf 4h" in log and f"data dir: {tmp_path}" in log
    assert (tmp_path / "reports" / "SOLUSDT_4h_2026-01-01_0000.md").exists()
    assert mgr.list()[0]["id"] == job["id"] and mgr.get("../../etc") is None and mgr.get("20260101-000000-abcdef") is None
    ids = [mgr.submit("review", {})["id"] for _ in range(4)]                       # ids sort in submission order
    assert ids == sorted(ids) and len(set(ids)) == 4


def test_failed_timeout_and_cancel(mgr):
    failed = wait_for(mgr, mgr.submit("audit", {"coins": "SOL", "strategies": "fail"})["id"])
    assert failed["status"] == "failed" and failed["returncode"] == 3 and "exit code 3" in failed["error"]
    slow = wait_for(mgr, mgr.submit("audit", {"coins": "SOL", "strategies": "sleep"})["id"], timeout=30)
    assert slow["status"] == "timeout" and "stopped after" in slow["error"]
    j = mgr.submit("review", {})
    long = mgr.submit("audit", {"coins": "ETH", "strategies": "sleep"})
    assert mgr.cancel(long["id"]) and wait_for(mgr, long["id"])["status"] in {"cancelled", "timeout"}
    wait_for(mgr, j["id"])
    assert not mgr.cancel(j["id"])                                        # already finished


def test_failure_reason_is_extracted_from_the_log(mgr):
    unlisted = wait_for(mgr, mgr.submit("audit", {"coins": "SOL", "strategies": "unlisted"})["id"])
    assert unlisted["status"] == "failed" and unlisted["error"] == "XUSDT is not listed on Binance Spot."
    blocked = wait_for(mgr, mgr.submit("audit", {"coins": "SOL", "strategies": "blocked"})["id"])
    assert blocked["status"] == "failed" and "Frankfurt or Singapore" in blocked["error"] and "HTTP 451" in blocked["error"]


def test_cancel_a_running_job(tmp_path, fake_audit):
    m = jobs.JobManager(tmp_path, fake_audit, poll=0.05, timeouts={"audit": 600})
    m.start()
    try:
        j = m.submit("audit", {"coins": "SOL", "strategies": "sleep"})
        wait_for(m, j["id"], {"running"})
        assert m.cancel(j["id"])
        done = wait_for(m, j["id"])
        assert done["status"] == "cancelled" and "cancelled by you" in done["error"]
    finally:
        m.stop()


def test_queue_limit(tmp_path, fake_audit):
    m = jobs.JobManager(tmp_path, fake_audit, poll=0.05, max_queue=1, timeouts={"audit": 600})
    m.start()
    try:
        first = m.submit("audit", {"coins": "SOL", "strategies": "sleep"})
        wait_for(m, first["id"], {"running"})
        m.submit("audit", {"coins": "ETH", "strategies": "sleep"})                # waits
        with pytest.raises(jobs.QueueFull):
            m.submit("audit", {"coins": "ADA", "strategies": "sleep"})
        assert m.live_counts() == {"queued": 1, "running": 1}
    finally:
        m.stop()


def test_restart_recovery(tmp_path, fake_audit):
    first = jobs.JobManager(tmp_path, fake_audit, poll=0.05)
    (tmp_path / "jobs").mkdir(parents=True)
    for jid, status in (("20260101-000000-000001", "running"), ("20260101-000001-000002", "queued")):
        d = tmp_path / "jobs" / jid
        d.mkdir()
        (d / "job.json").write_text(json.dumps({"id": jid, "kind": "audit", "label": "x", "args": ["SOL", "--tf", "4h"], "source": "web",
                                                "status": status, "created": "t", "started": None, "finished": None,
                                                "returncode": None, "reports": [], "error": ""}))
        (d / "output.log").write_bytes(b"")
    first.start()
    try:
        assert first.get("20260101-000000-000001")["status"] == "failed"
        assert "restart" in first.get("20260101-000000-000001")["error"]
        assert wait_for(first, "20260101-000001-000002")["status"] == "done"
    finally:
        first.stop()


def test_prune_jobs_keeps_newest(tmp_path, fake_audit):
    m = jobs.JobManager(tmp_path, fake_audit, poll=0.05, keep=3)
    m.start()
    try:
        ids = [m.submit("review", {})["id"] for _ in range(5)]
        wait_for(m, ids[-1])                       # jobs run one at a time, so the last one finishing means all did
        m.prune_jobs()
        assert [j["id"] for j in m.list()] == sorted(ids, reverse=True)[:3]        # older finished jobs were removed
    finally:
        m.stop()


# ---------------------------------------------------------- report retention
def test_prune_reports_keeps_newest_set_per_coin(tmp_path):
    now = 1_800_000_000
    old = now - 30 * 86400
    files = ["SOLUSDT_4h_2026-01-01_0000.md", "SOLUSDT_4h_2026-01-01_0000.json", "SOLUSDT_4h_2026-01-01_0000_tearsheet.html",
             "SOLUSDT_4h_2026-01-02_0000.md", "SOLUSDT_4h_2026-01-02_0000.json",
             "ETHUSDT_4h_2026-01-01_0000.md", "ETHUSDT_1h_2026-01-01_0000.md",
             "scan_4h_2026-01-01_0000.md", "scan_4h_2026-01-02_0000.md", "dashboard.html", "notes.txt"]
    for name in files:
        p = tmp_path / name
        p.write_text("x")
        os.utime(p, (old, old))
    recent = tmp_path / "SOLUSDT_4h_2026-01-02_0000.md"
    os.utime(recent, (now, now))
    removed = jobs.prune_reports(tmp_path, keep_days=14, now=now)
    left = {p.name for p in tmp_path.iterdir()}
    assert removed == 4
    assert left == {"SOLUSDT_4h_2026-01-02_0000.md", "SOLUSDT_4h_2026-01-02_0000.json", "ETHUSDT_4h_2026-01-01_0000.md",
                    "ETHUSDT_1h_2026-01-01_0000.md", "scan_4h_2026-01-02_0000.md", "dashboard.html", "notes.txt"}
    assert jobs.prune_reports(tmp_path, keep_days=0, now=now) == 0


# ---------------------------------------------------------------- scheduling
H = 3600


def test_is_due_on_candle_close_and_by_hours():
    base = 1_800_000_000 - (1_800_000_000 % (4 * H))                      # a 4h boundary
    assert jobs.is_due("candle:4h", 0, base + 120)                        # never ran, 2 min after the close
    assert not jobs.is_due("candle:4h", base + 70, base + 3600)           # already ran for this candle
    assert jobs.is_due("candle:4h", base + 70, base + 4 * H + 61)         # next candle closed, grace passed
    assert not jobs.is_due("candle:4h", base + 70, base + 4 * H + 30)     # still inside the grace window
    assert jobs.is_due("hours:24", 0, base) and not jobs.is_due("hours:24", base, base + 23 * H)
    assert jobs.is_due("hours:24", base, base + 24 * H) and not jobs.is_due("weekly:1", 0, base)


class StubManager:
    def __init__(self, tmp_path):
        self.data_dir, self.jobs, self.n = tmp_path, {}, 0

    def submit(self, kind, params, source="web"):
        self.n += 1
        jid = f"20260101-0000{self.n:02d}-000000"
        self.jobs[jid] = {"id": jid, "status": "queued", "kind": kind, "params": params, "source": source}
        return self.jobs[jid]

    def get(self, jid):
        return self.jobs.get(jid)


def test_scheduler_runs_each_schedule_once_per_period(tmp_path):
    clock = [1_800_000_000 - (1_800_000_000 % (4 * H)) + 120]
    m = StubManager(tmp_path)
    sc = jobs.Scheduler(m, [{"name": "watchlist", "kind": "audit", "when": "candle:4h", "params": {"coins": ["SOL"], "tf": "4h"}},
                            {"name": "review", "kind": "review", "when": "hours:24", "params": {}}],
                        tmp_path / "scheduler.json", clock=lambda: clock[0], grace=60)
    first = sc.tick()
    assert len(first) == 2 and sc.tick() == []                             # nothing new until a period passes
    assert json.loads((tmp_path / "scheduler.json").read_text())["watchlist"]["job"] == first[0]
    clock[0] += 4 * H
    assert sc.tick() == []                                                 # previous jobs still queued -> never pile up
    for j in m.jobs.values():
        j["status"] = "done"
    again = sc.tick()
    assert len(again) == 1 and m.jobs[again[0]]["kind"] == "audit"        # the 4h schedule only; review is 24h
    sc2 = jobs.Scheduler(m, sc.schedules, tmp_path / "scheduler.json", clock=lambda: clock[0], grace=60)
    assert sc2.tick() == []                                                # state survives a restart
    assert sc2.status()[0]["last_run"] != "never" and sc2.status()[0]["name"] == "watchlist"


def test_scheduler_skips_when_disk_is_full(tmp_path):
    m = StubManager(tmp_path)
    sc = jobs.Scheduler(m, [{"name": "watchlist", "kind": "audit", "when": "hours:1", "params": {}},
                            {"name": "review", "kind": "review", "when": "hours:1", "params": {}}],
                        tmp_path / "s.json", clock=lambda: 1e9, min_free_mb=10**9)
    ids = sc.tick()
    assert [m.jobs[i]["kind"] for i in ids] == ["review"]                  # reviews are cheap and keep running


def test_schedules_from_env():
    s, w = jobs.schedules_from_env({"COIN_AUDIT_WATCHLIST": "sol, arb", "COIN_AUDIT_WATCH_TF": "1h", "COIN_AUDIT_SCAN_HOURS": "12",
                                    "COIN_AUDIT_SCAN_N": "15", "DISCORD_WEBHOOK_URL": "https://x.invalid/h"})
    by = {x["name"]: x for x in s}
    assert w == [] and by["watchlist"]["when"] == "candle:1h" and by["watchlist"]["params"]["coins"] == ["SOL", "ARB"]
    assert by["watchlist"]["params"]["notify"] is True and by["scan"]["when"] == "hours:12" and by["review"]["when"] == "hours:24"
    s2, w2 = jobs.schedules_from_env({"COIN_AUDIT_WATCHLIST": "bad$coin", "COIN_AUDIT_SCAN_HOURS": "soon", "COIN_AUDIT_REVIEW_HOURS": "0"})
    assert s2 == [] and len(w2) == 2
    s3, w3 = jobs.schedules_from_env({"COIN_AUDIT_WATCHLIST": "SOL", "COIN_AUDIT_WATCH_TF": "all"})
    assert w3 and s3[0]["when"] == "hours:4"
    assert not jobs.notify_configured({}) and jobs.notify_configured({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"})
