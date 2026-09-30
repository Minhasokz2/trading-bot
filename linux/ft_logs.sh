#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
cd freqtrade
docker compose logs --tail 100 -f
