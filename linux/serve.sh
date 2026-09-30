#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# Run the web app on your own machine: http://127.0.0.1:10000   (user "admin")
# ./linux/serve.sh                        -> random password, printed below
# COIN_AUDIT_PASSWORD=mypass ./linux/serve.sh
if [ -z "$COIN_AUDIT_PASSWORD" ]; then
  COIN_AUDIT_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)"
  echo "Login: admin / $COIN_AUDIT_PASSWORD"
fi
export COIN_AUDIT_PASSWORD
HOST=127.0.0.1 PORT="${PORT:-10000}" .venv/bin/python audit/serve.py
