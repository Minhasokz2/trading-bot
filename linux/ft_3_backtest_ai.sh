#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# slow: 30-90 minutes
cd freqtrade
docker compose run --rm freqtrade backtesting --config user_data/config.json --config user_data/config-freqai.json --strategy CoinAuditFreqAI --freqaimodel LightGBMClassifier --timerange 20250601- --breakdown month
