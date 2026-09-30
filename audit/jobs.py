"""Background jobs and scheduling for the hosted web app (v6.1).

The web process stays small: it never imports pandas or LightGBM. Every audit runs as a separate
`python audit/audit.py ...` subprocess, so a crash or an out-of-memory kill in one job cannot take the
server down, and the memory is handed back to the OS when the job ends.

Files under COIN_AUDIT_DATA_DIR:
  jobs/<id>/job.json     status, arguments, timing, the reports the job produced
  jobs/<id>/output.log   the subprocess's combined stdout / stderr
  scheduler.json         when each schedule last ran (so a restart does not repeat work)
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

TIMEFRAMES = ("15m", "1h", "4h", "1d")
TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
MAX_COINS = 10
COIN_RE = re.compile(r"^[A-Z0-9]{2,15}$")
FILTER_RE = re.compile(r"^[A-Za-z0-9_,. \-]{1,200}$")
JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-\d{6}$")            # YYYYMMDD-HHMMSS-microseconds: sorts in submission order
TERMINAL = {"done", "failed", "cancelled", "timeout"}
DEFAULT_TIMEOUT_S = {"audit": 45 * 60, "scan": 6 * 3600, "review": 30 * 60, "demo": 15 * 60}
ERROR_RE = re.compile(r"^\s*ERROR:\s*(.+)$", re.M)
REGION_BLOCK = ("restricted location", "HTTP 451", " 451 ", "Error 451", "status code 451")
REGION_HINT = ("Binance refused this server's region (HTTP 451). Open Connectivity; if it says blocked, "
               "create the service again in Frankfurt or Singapore — a region cannot be changed afterwards.")
REPORT_RE = re.compile(r"(?:Report|Scan report):\s+(\S.*?\.md)\s*$", re.M)
DEMO_STRATEGIES = "trend_donchian_v1,range_support_v1,trend_ema_adx_v1"


class JobError(ValueError):
    """The request is invalid (bad symbol, timeframe, ...)."""


class QueueFull(RuntimeError):
    """Too many jobs are already waiting."""


def _bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "on", "yes", "y"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_coins(value) -> list[str]:
    parts = re.split(r"[\s,;]+", value.strip().upper()) if isinstance(value, str) else [str(x).strip().upper() for x in value]
    coins = [p for p in parts if p]
    if not coins:
        raise JobError("Enter at least one coin, for example SOL.")
    if len(coins) > MAX_COINS:
        raise JobError(f"At most {MAX_COINS} coins per job.")
    bad = [c for c in coins if not COIN_RE.match(c)]
    if bad:
        raise JobError("Not a valid coin symbol: " + ", ".join(bad[:3]) + ". Use letters and digits only, e.g. SOL.")
    return list(dict.fromkeys(coins))


def build_args(kind: str, p: dict) -> tuple[list[str], str]:
    """Validated command-line arguments for audit/audit.py (never a shell string) and a label."""
    tf = str(p.get("tf") or "4h").strip().lower()
    common: list[str] = []
    if not _bool(p.get("ml"), False):
        common.append("--no-ml")
    if not _bool(p.get("market"), True):
        common.append("--no-market")
    if not _bool(p.get("futures"), True):
        common.append("--no-futures")
    if not _bool(p.get("tearsheet"), False):
        common.append("--no-tearsheet")
    if _bool(p.get("notify"), False):
        common.append("--notify")
    flt = str(p.get("strategies") or "").strip()
    if flt:
        if not FILTER_RE.match(flt):
            raise JobError("The strategy filter may only contain letters, digits, spaces and _ , . -")
        common += ["--strategies", flt]
    if kind == "audit":
        coins = parse_coins(p.get("coins"))
        if tf not in TIMEFRAMES + ("all",):
            raise JobError("Timeframe must be 15m, 1h, 4h, 1d or all.")
        return [*coins, "--tf", tf, "--dashboard", *common], f"Audit {' '.join(coins)} · {tf}"
    if kind == "scan":
        try:
            n = int(p.get("n") or 20)
            jobs_n = int(p.get("jobs") or 1)
        except (TypeError, ValueError):
            raise JobError("The number of coins and processes must be whole numbers.") from None
        if not 2 <= n <= 60:
            raise JobError("Scan between 2 and 60 coins.")
        if not 1 <= jobs_n <= 4:
            raise JobError("Use 1 to 4 parallel processes.")
        if tf not in TIMEFRAMES:
            raise JobError("A scan needs one timeframe: 15m, 1h, 4h or 1d.")
        return ["--scan", str(n), "--tf", tf, "--jobs", str(jobs_n), "--dashboard", *common], f"Scan top {n} · {tf}"
    if kind == "review":
        return ["--review", "--dashboard"], "Review past audits"
    if kind == "demo":
        return (["--demo", "--tf", "4h", "--no-ml", "--no-tearsheet", "--dashboard", "--strategies", DEMO_STRATEGIES],
                "Demo check (synthetic data, no internet needed)")
    raise JobError(f"Unknown job type: {kind}")


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _kill(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """Stop the job's whole process group (the audit may have worker processes of its own)."""
    def send(sig):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
            except OSError:
                pass
    send(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        send(getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


class JobManager:
    def __init__(self, data_dir, audit_py, *, max_concurrent: int = 1, max_queue: int = 20, keep: int = 200,
                 python: str | None = None, timeouts: dict | None = None, extra_env: dict | None = None,
                 poll: float = 0.25):
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / "jobs"
        self.audit_py = str(audit_py)
        self.max_concurrent, self.max_queue, self.keep = max(1, max_concurrent), max_queue, keep
        self.python = python or sys.executable
        self.timeouts = {**DEFAULT_TIMEOUT_S, **(timeouts or {})}
        self.extra_env = dict(extra_env or {})
        self.poll = poll
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancel: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._last_us = 0

    def _new_id(self) -> str:
        """Strictly increasing id (call under the lock), so 'newest' always means 'submitted last'."""
        now = datetime.now(timezone.utc)
        us = int(now.timestamp() * 1_000_000)
        self._last_us = max(us, self._last_us + 1)
        t = datetime.fromtimestamp(self._last_us / 1_000_000, timezone.utc)
        return f"{t:%Y%m%d-%H%M%S}-{t.microsecond:06d}"

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for jd in sorted(p for p in self.dir.iterdir() if p.is_dir()):      # recover after a restart
            job = _read_json(jd / "job.json")
            if not job:
                continue
            if job["status"] == "running":
                self._finish(job, "failed", None, error="interrupted by a server restart")
            elif job["status"] == "queued":
                self._q.put(job["id"])
        self._stop.clear()
        for i in range(self.max_concurrent):
            t = threading.Thread(target=self._worker, name=f"job-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            procs = list(self._procs.values())
        for p in procs:
            _kill(p, grace=3.0)
        for t in self._threads:
            t.join(timeout=10)
        self._threads.clear()

    # ------------------------------------------------------------------ API
    def submit(self, kind: str, params: dict | None = None, source: str = "web") -> dict:
        args, label = build_args(kind, params or {})
        with self._lock:
            waiting = sum(1 for j in self.list(500) if j["status"] == "queued")
            if waiting >= self.max_queue:
                raise QueueFull(f"{waiting} jobs are already waiting — try again when some have finished.")
            jid = self._new_id()
            jd = self.dir / jid
            jd.mkdir(parents=True, exist_ok=True)
            job = {"id": jid, "kind": kind, "label": label, "args": args, "source": source, "status": "queued",
                   "created": now_iso(), "started": None, "finished": None, "returncode": None,
                   "reports": [], "error": ""}
            _write_json(jd / "job.json", job)
            (jd / "output.log").write_bytes(b"")
            self._q.put(jid)
        return job

    def get(self, jid: str) -> dict | None:
        if not JOB_ID_RE.match(jid or ""):
            return None
        return _read_json(self.dir / jid / "job.json")

    def list(self, limit: int = 50) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for jd in sorted((p for p in self.dir.iterdir() if p.is_dir()), reverse=True)[:limit]:
            job = _read_json(jd / "job.json")
            if job:
                out.append(job)
        return out

    def log_tail(self, jid: str, max_bytes: int = 60_000) -> str:
        if not JOB_ID_RE.match(jid or ""):
            return ""
        try:
            with (self.dir / jid / "output.log").open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                data = fh.read()
        except OSError:
            return ""
        text = data.decode("utf-8", "replace")
        return ("… (earlier output trimmed)\n" if size > max_bytes else "") + text

    def cancel(self, jid: str) -> bool:
        job = self.get(jid)
        if not job or job["status"] in TERMINAL:
            return False
        if job["status"] == "queued":
            self._finish(job, "cancelled", None, error="cancelled before it started")
            return True
        with self._lock:
            self._cancel.add(jid)                    # the worker notices within one poll and stops the process group
        return True

    def live_counts(self) -> dict:
        """Cheap in-memory counts (health checks call this every few seconds)."""
        with self._lock:
            return {"queued": self._q.qsize(), "running": len(self._procs)}

    def counts(self) -> dict:
        jobs = self.list(500)
        return {"queued": sum(j["status"] == "queued" for j in jobs), "running": sum(j["status"] == "running" for j in jobs)}

    # ------------------------------------------------------------- internals
    def _finish(self, job: dict, status: str, rc, error: str = "") -> dict:
        job.update({"status": status, "returncode": rc, "finished": now_iso(), "error": error})
        log = self.log_tail(job["id"], 400_000)
        job["reports"] = list(dict.fromkeys(Path(m).name for m in REPORT_RE.findall(log)))
        if status == "failed":
            errs = ERROR_RE.findall(log)
            if any(k in log for k in REGION_BLOCK):
                job["error"] = REGION_HINT
            elif errs:
                job["error"] = (f"{len(errs)} error(s), last: " if len(errs) > 1 else "") + errs[-1].strip()[:300]
        _write_json(self.dir / job["id"] / "job.json", job)
        self.prune_jobs()
        return job

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                jid = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run(jid)
            except Exception as e:                                         # a bug here must not kill the worker
                job = self.get(jid)
                if job and job["status"] not in TERMINAL:
                    self._finish(job, "failed", None, error=f"runner error: {e}")

    def _run(self, jid: str) -> None:
        job = self.get(jid)
        if not job or job["status"] != "queued":
            return
        job.update({"status": "running", "started": now_iso()})
        _write_json(self.dir / jid / "job.json", job)
        env = {**os.environ, **self.extra_env, "COIN_AUDIT_DATA_DIR": str(self.data_dir), "PYTHONUNBUFFERED": "1"}
        timeout = self.timeouts.get(job["kind"], 3600)
        with (self.dir / jid / "output.log").open("ab") as log:
            log.write(f"$ audit.py {' '.join(job['args'])}\n".encode())
            log.flush()
            proc = subprocess.Popen([self.python, "-u", self.audit_py, *job["args"]], stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env, start_new_session=True)
            with self._lock:
                self._procs[jid] = proc
            t0, status, err = time.time(), None, ""
            while proc.poll() is None:
                if self._stop.is_set():
                    _kill(proc); status, err = "cancelled", "server shutting down"; break
                with self._lock:
                    cancelled = jid in self._cancel
                if cancelled:
                    _kill(proc); status, err = "cancelled", "cancelled by you"; break
                if time.time() - t0 > timeout:
                    _kill(proc); status, err = "timeout", f"stopped after {timeout // 60} minutes"; break
                time.sleep(self.poll)
            with self._lock:
                self._procs.pop(jid, None)
                if jid in self._cancel and status is None:
                    status, err = "cancelled", "cancelled by you"
                self._cancel.discard(jid)
        rc = proc.returncode
        if status is None:
            status = "done" if rc == 0 else "failed"
            err = "" if rc == 0 else (f"exit code {rc}" + (" (killed — most likely out of memory: use a plan with more RAM)" if rc in (-9, 137)
                                                          else " (the audit reported an error — see the log)" if rc == 2 else ""))
        self._finish(job, status, rc, error=err)

    def prune_jobs(self) -> int:
        if not self.dir.exists():
            return 0
        dirs = sorted((p for p in self.dir.iterdir() if p.is_dir()), reverse=True)
        removed = 0
        for jd in dirs[self.keep:]:
            job = _read_json(jd / "job.json")
            if job is None:
                if time.time() - jd.stat().st_mtime < 120:      # a job that is being created right now
                    continue
            elif job["status"] not in TERMINAL:
                continue
            shutil.rmtree(jd, ignore_errors=True)
            removed += 1
        return removed


# ------------------------------------------------------------ report retention
_REPORT_BASE = re.compile(r"^(?P<key>(?:[A-Z0-9]+_(?:15m|1h|4h|1d))|(?:scan_(?:15m|1h|4h|1d)))_(?P<stamp>\d{4}-\d{2}-\d{2}_\d{4})(?P<rev>_r\d+)?")


def prune_reports(reports_dir, keep_days: float = 14, now: float | None = None) -> int:
    """Delete report files older than keep_days, always keeping the newest audit set of every
    (symbol, timeframe) and of every scan timeframe. Returns the number of files removed."""
    reports_dir = Path(reports_dir)
    if not reports_dir.exists() or keep_days <= 0:
        return 0
    now = time.time() if now is None else now
    sets: dict[str, dict[tuple, list[Path]]] = {}
    for f in reports_dir.iterdir():
        m = _REPORT_BASE.match(f.name)
        if f.is_file() and m:
            sets.setdefault(m["key"], {}).setdefault((m["stamp"], m["rev"] or ""), []).append(f)
    removed = 0
    for versions in sets.values():
        newest = max(versions)
        for ver, files in versions.items():
            if ver == newest:
                continue
            for f in files:
                if now - f.stat().st_mtime > keep_days * 86400:
                    f.unlink(missing_ok=True)
                    removed += 1
    return removed


# --------------------------------------------------------------- scheduling
def notify_configured(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("DISCORD_WEBHOOK_URL") or (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")))


def schedules_from_env(env=None) -> tuple[list[dict], list[str]]:
    """The scheduled jobs described by environment variables, and human-readable warnings for bad values.
    COIN_AUDIT_WATCHLIST=SOL,ARB  COIN_AUDIT_WATCH_TF=4h  COIN_AUDIT_SCHEDULE_ML=0
    COIN_AUDIT_SCAN_HOURS=24  COIN_AUDIT_SCAN_N=20  COIN_AUDIT_SCAN_TF=4h  COIN_AUDIT_REVIEW_HOURS=24"""
    env = os.environ if env is None else env
    out, warn = [], []
    ml, notify = _bool(env.get("COIN_AUDIT_SCHEDULE_ML"), False), notify_configured(env)
    wl = env.get("COIN_AUDIT_WATCHLIST", "").strip()
    if wl:
        tf = (env.get("COIN_AUDIT_WATCH_TF") or "4h").strip().lower()
        try:
            params = {"coins": parse_coins(wl), "tf": tf, "ml": ml, "notify": notify}
            build_args("audit", params)
            out.append({"name": "watchlist", "kind": "audit", "when": f"candle:{tf}" if tf in TF_SECONDS else "hours:4",
                        "params": params})
            if tf not in TF_SECONDS:
                warn.append(f"COIN_AUDIT_WATCH_TF={tf!r}: 'all' runs every 4 hours instead of on a candle close.")
        except JobError as e:
            warn.append(f"COIN_AUDIT_WATCHLIST ignored: {e}")
    try:
        scan_h = float(env.get("COIN_AUDIT_SCAN_HOURS") or 0)
    except ValueError:
        scan_h = 0
        warn.append("COIN_AUDIT_SCAN_HOURS must be a number of hours.")
    if scan_h > 0:
        params = {"n": env.get("COIN_AUDIT_SCAN_N") or 20, "tf": (env.get("COIN_AUDIT_SCAN_TF") or "4h").lower(),
                  "ml": ml, "notify": notify}
        try:
            build_args("scan", params)
            out.append({"name": "scan", "kind": "scan", "when": f"hours:{scan_h:g}", "params": params})
        except JobError as e:
            warn.append(f"Scheduled scan ignored: {e}")
    try:
        rev_h = float(env.get("COIN_AUDIT_REVIEW_HOURS") or 24)
    except ValueError:
        rev_h = 24
        warn.append("COIN_AUDIT_REVIEW_HOURS must be a number of hours; using 24.")
    if rev_h > 0:
        out.append({"name": "review", "kind": "review", "when": f"hours:{rev_h:g}", "params": {}})
    return out, warn


def is_due(when: str, last_run: float, now: float, grace: float = 60.0) -> bool:
    kind, _, arg = when.partition(":")
    if kind == "candle":
        step = TF_SECONDS[arg]
        trigger = (now // step) * step + grace
        if now < trigger:                              # still inside the grace window of the newest close
            trigger -= step
        return last_run < trigger
    if kind == "hours":
        return now - last_run >= float(arg) * 3600
    return False


def describe(when: str) -> str:
    kind, _, arg = when.partition(":")
    return f"{arg} candle close" if kind == "candle" else f"every {arg} h"


class Scheduler:
    def __init__(self, manager: JobManager, schedules: list[dict], state_path, *, clock=time.time, grace: float = 60.0,
                 min_free_mb: int = 100, reports_dir=None, keep_days: float = 14):
        self.manager, self.schedules = manager, schedules
        self.state_path = Path(state_path)
        self.clock, self.grace, self.min_free_mb = clock, grace, min_free_mb
        self.reports_dir, self.keep_days = reports_dir, keep_days
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune = 0.0

    def state(self) -> dict:
        return _read_json(self.state_path) or {}

    def status(self) -> list[dict]:
        st, now = self.state(), self.clock()
        rows = []
        for s in self.schedules:
            last = (st.get(s["name"]) or {}).get("last_run", 0)
            rows.append({"name": s["name"], "kind": s["kind"], "when": describe(s["when"]),
                         "last_run": datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%d %H:%M") if last else "never",
                         "due": is_due(s["when"], last, now, self.grace), "params": s["params"]})
        return rows

    def tick(self) -> list[str]:
        """Submit every schedule that is due (and whose previous job has finished). Returns the new job ids."""
        now, st, submitted = self.clock(), self.state(), []
        free_mb = shutil.disk_usage(self.manager.data_dir).free / 1e6 if self.manager.data_dir.exists() else 1e9
        for s in self.schedules:
            entry = st.get(s["name"]) or {}
            if not is_due(s["when"], entry.get("last_run", 0), now, self.grace):
                continue
            prev = self.manager.get(entry.get("job", ""))
            if prev and prev["status"] not in TERMINAL:
                continue                                  # the last run is still going — never pile up
            if free_mb < self.min_free_mb and s["kind"] != "review":
                continue                                  # disk almost full: skip, the home page shows a warning
            try:
                job = self.manager.submit(s["kind"], s["params"], source=f"schedule:{s['name']}")
            except (JobError, QueueFull):
                continue
            st[s["name"]] = {"last_run": now, "job": job["id"]}
            submitted.append(job["id"])
        if submitted:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(self.state_path, st)
        if self.reports_dir and now - self._last_prune > 6 * 3600:
            prune_reports(self.reports_dir, self.keep_days, now)
            self._last_prune = now
        return submitted

    def start(self, interval: float = 30.0) -> None:
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:                          # never let the scheduler thread die
                    pass
        try:
            self.tick()
        except Exception:
            pass
        self._thread = threading.Thread(target=loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
