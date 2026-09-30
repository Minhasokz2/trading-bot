#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# One-time setup: finds Python 3.11-3.14, makes .venv, installs requirements
PY=""
for c in python3.12 python3.13 python3.11 python3.14 python3; do
  if command -v $c >/dev/null && $c -c "import sys; sys.exit(0 if (3,11)<=sys.version_info[:2]<(3,15) else 1)"; then PY=$c; break; fi
done
[ -z "$PY" ] && { echo "Python 3.11-3.14 not found. See README (Linux section)."; exit 1; }
echo "Using $($PY --version)"
$PY -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo; echo "Done. Try:  ./linux/demo.sh   (offline self-check)   then   ./linux/audit.sh SOL"
