#!/bin/bash
# macOS launcher — double-click in Finder (Terminal opens) or run from Terminal.
cd "$(dirname "$0")/.." || exit 1
done_msg(){ echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }
[ -x .venv/bin/python ] || { echo "Run \"1 Install.command\" first."; done_msg; exit 1; }
read -r -p "Coin(s) to watch, e.g. SOL ARB : " COINS; [ -z "$COINS" ] && { echo "No coin given."; done_msg; exit 1; }
read -r -p "Timeframe [15m/1h/4h/1d] (Enter = 4h): " TF; TF="${TF:-4h}"
echo "Re-audits after every $TF candle close and rebuilds the dashboard. Press Ctrl-C to stop."
.venv/bin/python audit/audit.py $COINS --tf "$TF" --loop --dashboard
done_msg
