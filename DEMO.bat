@echo off
REM  Full audit on synthetic data (no network): proves the install works end to end
cd /d "%~dp0"
.venv\Scripts\python audit\audit.py --demo %*
pause
