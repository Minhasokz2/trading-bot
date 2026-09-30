@echo off
REM  Rebuild audit\reports\dashboard.html from the reports already on disk and open it
cd /d "%~dp0"
.venv\Scripts\python audit\audit.py --dashboard
start "" "audit\reports\dashboard.html"
pause
