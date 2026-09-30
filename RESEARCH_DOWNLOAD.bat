@echo off
REM  RESEARCH_DOWNLOAD.bat SOL   (official Binance bulk files, 24 months of 4h + 1h + 1d)
cd /d "%~dp0"
.venv\Scripts\python research\download_history.py %* --tf 1h 4h 1d --months 24
pause
