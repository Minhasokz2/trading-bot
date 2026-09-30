#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# Backtest every rule strategy on its default timeframe and print one comparison table
cd freqtrade
for p in CoinAuditTrend:4h CoinAuditDonchian:4h CoinAuditPullback:1h CoinAuditSMAOffset:1h CoinAuditBinCluc:1h CoinAuditElliot:1h CoinAuditConfluence:4h CoinAuditEmaCross:1h; do
  s=${p%%:*}; t=${p##*:}
  docker compose run --rm freqtrade backtesting --config user_data/config.json --strategy "$s" --timeframe "$t" --timerange 20250101- --enable-protections 2>&1 | grep -E "│ +$s " || echo "  $s: no result"
done
