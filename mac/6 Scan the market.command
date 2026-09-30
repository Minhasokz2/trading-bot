#!/bin/bash
# macOS launcher — double-click in Finder (Terminal opens) or run from Terminal.
cd "$(dirname "$0")/.." || exit 1
done_msg(){ echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }
[ -x .venv/bin/python ] || { echo "Run \"1 Install.command\" first."; done_msg; exit 1; }
read -r -p "How many coins (top by 24h volume) [40]: " N; N="${N:-40}"
read -r -p "Timeframe [15m/1h/4h/1d] (Enter = 4h): " TF; TF="${TF:-4h}"
.venv/bin/python audit/audit.py --scan "$N" --tf "$TF" --dashboard
read -r -p "Open the reports folder? [Y/n] " ans; [[ "$ans" =~ ^[Nn] ]] || open audit/reports
done_msg
