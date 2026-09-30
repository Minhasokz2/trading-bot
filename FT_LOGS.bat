@echo off
cd /d "%~dp0freqtrade"
docker compose logs --tail 100 -f
