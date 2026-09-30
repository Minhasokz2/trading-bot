@echo off
REM  Offline self-test: synthetic data, planted patterns, lookahead checks, full pipeline (no network needed)
cd /d "%~dp0"
.venv\Scripts\python -m pytest -q --timeout=2400
pause
