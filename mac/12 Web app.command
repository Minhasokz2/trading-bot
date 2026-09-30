#!/bin/bash
# macOS launcher — double-click in Finder (Terminal opens) or run from Terminal.
cd "$(dirname "$0")/.." || exit 1
done_msg(){ echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }
[ -x .venv/bin/python ] || { echo "Run \"1 Install.command\" first."; done_msg; exit 1; }
if [ -z "$COIN_AUDIT_PASSWORD" ]; then read -r -s -p "Choose a password for the web app: " COIN_AUDIT_PASSWORD; echo; fi
export COIN_AUDIT_PASSWORD HOST=127.0.0.1 PORT="${PORT:-10000}"
echo "Open http://127.0.0.1:$PORT  (user: admin). Press Ctrl-C to stop."
(sleep 3; open "http://127.0.0.1:$PORT") &
.venv/bin/python audit/serve.py
done_msg
