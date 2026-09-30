@echo off
REM  Run the web app on your own machine: http://127.0.0.1:10000  (user "admin")
cd /d "%~dp0"
if "%COIN_AUDIT_PASSWORD%"=="" set /p COIN_AUDIT_PASSWORD=Choose a password for the web app: 
set HOST=127.0.0.1
if "%PORT%"=="" set PORT=10000
echo Open http://127.0.0.1:%PORT%  ^(user: admin^)
.venv\Scripts\python audit\serve.py
pause
