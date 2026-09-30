#!/bin/bash
# macOS launcher — double-click in Finder (Terminal opens) or run from Terminal.
cd "$(dirname "$0")/.." || exit 1
done_msg(){ echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }
[ -x .venv/bin/python ] || { echo "Run \"1 Install.command\" first."; done_msg; exit 1; }
.venv/bin/python audit/audit.py --dashboard && open audit/reports/dashboard.html
done_msg
