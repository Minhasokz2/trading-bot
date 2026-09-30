#!/bin/bash
# macOS launcher — double-click in Finder (Terminal opens) or run from Terminal.
cd "$(dirname "$0")/.." || exit 1
done_msg(){ echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }
read -r -p "Coin, e.g. SOL : " COIN
read -r -p "Timeframe [1h/4h/1d] (Enter = 4h): " TF
.venv/bin/python research/sweep_vectorbt.py $COIN --tf ${TF:-4h}
done_msg
