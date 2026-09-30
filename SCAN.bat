@echo off
REM  SCAN.bat            -> audit the 40 most traded USDT pairs, rank them, suggest an allocation
REM  SCAN.bat 20 --tf 1h --jobs 4 --notify
cd /d "%~dp0"
set N=%1
if "%N%"=="" set N=40
if not "%1"=="" shift
.venv\Scripts\python audit\audit.py --scan %N% %1 %2 %3 %4 %5 %6 %7 %8
pause
