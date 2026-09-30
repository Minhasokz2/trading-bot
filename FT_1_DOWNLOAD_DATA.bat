@echo off
cd /d "%~dp0freqtrade"
docker compose run --rm freqtrade download-data --config user_data/config.json --timeframes 15m 1h 4h 1d --days 900
pause
