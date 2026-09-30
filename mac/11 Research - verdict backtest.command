#!/bin/bash
# macOS launcher — double-click in Finder (Terminal opens) or run from Terminal.
cd "$(dirname "$0")/.." || exit 1
done_msg(){ echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }
[ -x .venv/bin/python ] || { echo "Run \"1 Install.command\" first."; done_msg; exit 1; }
read -r -p "Coin to replay, e.g. SOL : " COIN; [ -z "$COIN" ] && { echo "No coin given."; done_msg; exit 1; }
read -r -p "Timeframe [4h]: " TF; TF="${TF:-4h}"
read -r -p "Days back [180]: " SPAN; SPAN="${SPAN:-180}"
.venv/bin/python research/verdict_backtest.py "$COIN" --tf "$TF" --span "$SPAN" --jobs 2
done_msg
