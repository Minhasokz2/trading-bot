@echo off
REM  AUDIT.bat SOL  (asks for timeframe)   AUDIT.bat SOL --tf all   AUDIT.bat --review
cd /d "%~dp0"
.venv\Scripts\python audit\audit.py %*
pause
