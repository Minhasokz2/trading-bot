#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# ./linux/audit.sh SOL            -> asks for the timeframe
# ./linux/audit.sh SOL --tf 1h    ./linux/audit.sh SOL --tf all    ./linux/audit.sh --review
.venv/bin/python audit/audit.py "$@"
