@echo off
echo Strategy: 1 Trend  2 Donchian  3 Pullback  4 SMAOffset  5 BinCluc  6 Elliot  7 Confluence  8 EmaCross
set S=1
set /p S=Choose [1-8] (Enter = 1): 
set STRATEGY=CoinAuditTrend& set DEF=4h
if "%S%"=="2" set STRATEGY=CoinAuditDonchian& set DEF=4h
if "%S%"=="3" set STRATEGY=CoinAuditPullback& set DEF=1h
if "%S%"=="4" set STRATEGY=CoinAuditSMAOffset& set DEF=1h
if "%S%"=="5" set STRATEGY=CoinAuditBinCluc& set DEF=1h
if "%S%"=="6" set STRATEGY=CoinAuditElliot& set DEF=1h
if "%S%"=="7" set STRATEGY=CoinAuditConfluence& set DEF=4h
if "%S%"=="8" set STRATEGY=CoinAuditEmaCross& set DEF=1h
set TIMEFRAME=%DEF%
set /p TIMEFRAME=Timeframe [15m/1h/4h/1d] (Enter = %DEF%): 
echo -^> %STRATEGY% on %TIMEFRAME%
cd /d "%~dp0freqtrade"
docker compose up -d
echo Dashboards: http://127.0.0.1:8080 (%STRATEGY% %TIMEFRAME%)   http://127.0.0.1:8081 (FreqAI)
pause
