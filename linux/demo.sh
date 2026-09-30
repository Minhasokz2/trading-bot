#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# Full audit on synthetic data (no network): proves the install works end to end
.venv/bin/python audit/audit.py --demo "$@"
