#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# Offline self-test: synthetic data, planted patterns, lookahead checks, full pipeline (no network needed)
.venv/bin/python -m pytest -q --timeout=2400 "$@"
