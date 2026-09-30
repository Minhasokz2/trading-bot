#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
pick(){ N=(CoinAuditTrend CoinAuditDonchian CoinAuditPullback CoinAuditSMAOffset CoinAuditBinCluc CoinAuditElliot CoinAuditConfluence CoinAuditEmaCross); T=(4h 4h 1h 1h 1h 1h 4h 1h); echo "Strategy:"; for i in "${!N[@]}"; do echo "  $((i+1))) ${N[$i]}  (default ${T[$i]})"; done; read -rp "Choose [1-8] (Enter = 1): " s; s=${s:-1}; [[ "$s" =~ ^[1-8]$ ]] || s=1; STRATEGY=${N[$((s-1))]}; read -rp "Timeframe [15m/1h/4h/1d] (Enter = ${T[$((s-1))]}): " TIMEFRAME; TIMEFRAME=${TIMEFRAME:-${T[$((s-1))]}}; echo "-> $STRATEGY on $TIMEFRAME"; }
pick
cd freqtrade
docker compose run --rm freqtrade backtesting --config user_data/config.json --strategy "$STRATEGY" --timeframe "$TIMEFRAME" --timerange 20250101- --enable-protections --breakdown month
