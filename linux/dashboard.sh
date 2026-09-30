#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# Rebuild audit/reports/dashboard.html from the reports and logs already on disk
.venv/bin/python audit/audit.py --dashboard
echo "Open audit/reports/dashboard.html in a browser."
