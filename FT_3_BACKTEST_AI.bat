@echo off
REM Backtest the FreqAI LightGBM strategy. Retrains every 7 days - can take 30-90 minutes.
cd /d "%~dp0freqtrade"
docker compose run --rm freqtrade backtesting --config user_data/config.json --config user_data/config-freqai.json --strategy CoinAuditFreqAI --freqaimodel LightGBMClassifier --timerange 20250601- --breakdown month
pause
