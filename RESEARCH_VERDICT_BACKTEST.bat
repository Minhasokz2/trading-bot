@echo off
REM  RESEARCH_VERDICT_BACKTEST.bat SOL --tf 4h --span 180 --every 7 --jobs 4
cd /d "%~dp0"
.venv\Scripts\python research\verdict_backtest.py %*
pause
