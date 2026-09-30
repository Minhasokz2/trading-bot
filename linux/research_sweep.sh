#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# ./linux/research_sweep.sh SOL
.venv/bin/python research/sweep_vectorbt.py "$@"
