#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# ./linux/watch.sh SOL ARB --tf 4h --notify   -> re-audit after every candle close, rebuild the dashboard (Ctrl-C stops)
.venv/bin/python audit/audit.py --loop --dashboard "$@"
