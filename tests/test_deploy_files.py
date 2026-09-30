"""The deployment files must agree with each other and with the code (no network, no Docker needed)."""
import ast
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import jobs
import webapp

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "audit"


def requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            names.add(line.replace("_", "-").lower().split(">")[0].split("=")[0].split("<")[0].strip())
    return names


def test_render_blueprint_matches_the_app():
    doc = yaml.safe_load((ROOT / "render.yaml").read_text())
    svc = doc["services"][0]
    assert svc["type"] == "web" and svc["runtime"] == "docker" and (ROOT / svc["dockerfilePath"]).exists()
    assert svc["region"] in {"frankfurt", "singapore"}, "Binance refuses some countries (HTTP 451), notably the US"
    assert svc["plan"] not in {"free", "starter"}, "the 512 MB plans cannot hold one audit (about 500 MB at peak)"
    env = {e["key"]: e for e in svc["envVars"]}
    assert env["COIN_AUDIT_DATA_DIR"]["value"] == svc["disk"]["mountPath"] == "/data"
    assert env["COIN_AUDIT_PASSWORD"]["generateValue"] is True and "value" not in env["COIN_AUDIT_PASSWORD"]
    assert svc["healthCheckPath"] in webapp.OPEN_PATHS, "the health check must not need a password"
    code = (AUDIT / "webapp.py").read_text() + (AUDIT / "jobs.py").read_text()
    for key in env:
        assert key in code, f"{key} is set in render.yaml but never read by the code"
    schedules, warnings = jobs.schedules_from_env({k: str(e.get("value", "")) for k, e in env.items()})
    assert warnings == [] and {s["name"] for s in schedules} == {"watchlist", "review"}
    assert svc["disk"]["sizeGB"] >= 1


def test_dockerfile_matches_the_blueprint_and_the_launcher():
    docker = (ROOT / "Dockerfile").read_text()
    assert docker.startswith("# Coin Audit") and "FROM python:3.12" in docker
    assert "pip install -r requirements-web.txt -c requirements-web-lock.txt" in docker
    assert "requirements.txt " not in docker.replace("requirements-", "")
    lock = requirement_names(ROOT / "requirements-web-lock.txt")
    assert {"fastapi", "uvicorn", "markdown", "pandas", "lightgbm", "statsmodels"} <= lock, "the lock must cover the web set"
    assert not ({"vectorbt", "pytest", "httpx"} & lock), "the hosted lock is resolved from requirements-web.txt only"
    assert "COPY audit ./audit" in docker and 'CMD ["python", "audit/serve.py"]' in docker
    assert "COIN_AUDIT_DATA_DIR=/data" in docker and "libgomp1" in docker, "LightGBM needs the OpenMP runtime"
    serve = (AUDIT / "serve.py").read_text()
    assert '"PORT", "10000"' in serve and "EXPOSE 10000" in docker
    ignore = (ROOT / ".dockerignore").read_text().split()
    for must in ("audit/reports", "audit/logs", "audit/cache", ".git", ".venv", "tests"):
        assert must in ignore
    assert (ROOT / "docker" / "entrypoint.sh").stat().st_mode & stat.S_IXUSR


def test_hosted_requirements_cover_every_import():
    """The image installs requirements-web.txt only. Every third-party module the audit/ code imports
    (including lazy imports inside functions) must be declared there, or the deploy would fail at runtime."""
    declared = requirement_names(ROOT / "requirements-web.txt") | requirement_names(ROOT / "requirements-core.txt")
    provided_by = {"pandas-ta-classic": "pandas_ta_classic", "scikit-learn": "sklearn", "binance-sdk-spot": "binance_sdk_spot",
                   "binance-sdk-derivatives-trading-usds-futures": "binance_sdk_derivatives_trading_usds_futures"}
    module_to_pkg = {v: k for k, v in provided_by.items()} | {"binance_common": "binance-sdk-spot"}
    local = {p.stem for p in AUDIT.glob("*.py")}
    missing = set()
    for py in AUDIT.glob("*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else []
            for m in mods:
                top = m.split(".")[0]
                if top in sys.stdlib_module_names or top in local:
                    continue
                pkg = module_to_pkg.get(top, top.replace("_", "-").lower())
                if pkg not in declared:
                    missing.add(f"{py.name}: {top} (needs {pkg})")
    assert not missing, sorted(missing)
    web_only = requirement_names(ROOT / "requirements-web.txt") | requirement_names(ROOT / "requirements-core.txt")
    assert not ({"vectorbt", "pytest", "httpx"} & web_only), "research and test packages do not belong in the hosted image"
    assert {"fastapi", "uvicorn", "markdown"} <= web_only
    full = requirement_names(ROOT / "requirements.txt")
    assert {"vectorbt", "pytest", "httpx"} <= full


def _run_entrypoint(tmp_path, *cmd, gosu: str | None = None):
    data = tmp_path / "data"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if gosu is not None:
        (bindir / "gosu").write_text(gosu)
        (bindir / "gosu").chmod(0o755)
    env = {**os.environ, "COIN_AUDIT_DATA_DIR": str(data), "PATH": f"{bindir}:{os.environ['PATH']}"}
    r = subprocess.run(["sh", str(ROOT / "docker" / "entrypoint.sh"), *cmd], capture_output=True, text=True, env=env, timeout=30)
    return r, data


def test_entrypoint_creates_the_data_dir_and_runs_the_command(tmp_path):
    r, data = _run_entrypoint(tmp_path, "echo", "started")
    assert r.returncode == 0 and "started" in r.stdout and data.is_dir()
    r2, _ = _run_entrypoint(tmp_path, "sh", "-c", "exit 7")
    assert r2.returncode == 7                                              # the app's exit code is passed through


@pytest.mark.skipif(os.geteuid() != 0, reason="the privilege-drop branch only runs as root (as in the container)")
def test_entrypoint_drops_privileges_with_gosu(tmp_path):
    fake = f'#!/bin/sh\necho "gosu $1" >> "{tmp_path}/gosu.log"\nshift\nexec "$@"\n'
    r, _ = _run_entrypoint(tmp_path, "echo", "served", gosu=fake)
    assert r.returncode == 0 and "served" in r.stdout
    calls = (tmp_path / "gosu.log").read_text().split("\n")
    assert calls.count("gosu app") >= 2                                    # once for the write test, once for the real command


@pytest.mark.skipif(os.geteuid() != 0, reason="the privilege-drop branch only runs as root (as in the container)")
def test_entrypoint_falls_back_to_root_when_the_disk_is_not_writable(tmp_path):
    r, _ = _run_entrypoint(tmp_path, "echo", "served-anyway", gosu="#!/bin/sh\nexit 1\n")
    assert r.returncode == 0 and "served-anyway" in r.stdout and "running as root instead" in r.stderr


def test_serve_launcher_reads_port_and_host(monkeypatch):
    import serve
    seen = {}
    monkeypatch.setitem(sys.modules, "uvicorn", type("U", (), {"run": staticmethod(lambda app, **kw: seen.update(app=app, **kw))}))
    monkeypatch.setenv("PORT", "12345")
    monkeypatch.delenv("HOST", raising=False)
    serve.main()
    assert seen["app"] == "webapp:app" and seen["port"] == 12345 and seen["host"] == "0.0.0.0"
    monkeypatch.setenv("HOST", "127.0.0.1")
    serve.main()
    assert seen["host"] == "127.0.0.1"
