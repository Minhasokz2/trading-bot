#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# ./linux/research_verdict_backtest.sh SOL --tf 4h --span 180 --every 7 --jobs 4
.venv/bin/python research/verdict_backtest.py "$@"
