@echo off
REM  WATCH.bat SOL ARB --tf 4h --notify  -> re-audit after every candle close and rebuild the dashboard (Ctrl-C stops)
cd /d "%~dp0"
.venv\Scripts\python audit\audit.py --loop --dashboard %*
pause
