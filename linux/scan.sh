#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# ./linux/scan.sh            -> audit the 40 most traded USDT pairs on 4h, rank them, suggest an allocation
# ./linux/scan.sh 20 --tf 1h --jobs 4 --notify
N="${1:-40}"; shift || true
.venv/bin/python audit/audit.py --scan "$N" --tf "${TF:-4h}" "$@"
