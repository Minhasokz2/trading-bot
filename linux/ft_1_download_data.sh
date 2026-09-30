#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
cd freqtrade
docker compose run --rm freqtrade download-data --config user_data/config.json --timeframes 15m 1h 4h 1d --days 900
