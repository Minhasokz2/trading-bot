@echo off
REM  RESEARCH_SWEEP.bat SOL   (VectorBT parameter sweep, in-sample vs out-of-sample)
cd /d "%~dp0"
.venv\Scripts\python research\sweep_vectorbt.py %*
pause
