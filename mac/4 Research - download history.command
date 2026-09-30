#!/bin/bash
# macOS launcher — double-click in Finder (Terminal opens) or run from Terminal.
cd "$(dirname "$0")/.." || exit 1
done_msg(){ echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }
read -r -p "Coin(s), e.g. SOL : " COINS
.venv/bin/python research/download_history.py $COINS --tf 1h 4h 1d --months 24
done_msg
