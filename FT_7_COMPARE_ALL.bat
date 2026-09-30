@echo off
cd /d "%~dp0freqtrade"
for %%P in (CoinAuditTrend:4h CoinAuditDonchian:4h CoinAuditPullback:1h CoinAuditSMAOffset:1h CoinAuditBinCluc:1h CoinAuditElliot:1h CoinAuditConfluence:4h CoinAuditEmaCross:1h) do for /f "tokens=1,2 delims=:" %%A in ("%%P") do docker compose run --rm freqtrade backtesting --config user_data/config.json --strategy %%A --timeframe %%B --timerange 20250101- --enable-protections
pause
