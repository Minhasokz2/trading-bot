@echo off
REM One-time setup of the audit tool and research scripts.
cd /d "%~dp0"
where py >nul 2>nul && (py -3.12 -m venv .venv || py -3 -m venv .venv) || python -m venv .venv
if not exist .venv\Scripts\python.exe (echo Python 3.11-3.14 not found. Install Python 3.12 from python.org, tick "Add to PATH", then rerun. & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
echo.
echo Done. Try:  DEMO.bat  (offline self-check)  then  AUDIT.bat SOL
pause
