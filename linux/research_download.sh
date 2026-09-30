#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# ./linux/research_download.sh SOL
.venv/bin/python research/download_history.py "$@" --tf 1h 4h 1d --months 24
