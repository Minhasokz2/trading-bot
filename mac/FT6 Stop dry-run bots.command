#!/bin/bash
# macOS launcher — double-click in Finder (Terminal opens) or run from Terminal.
cd "$(dirname "$0")/.." || exit 1
done_msg(){ echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop (or: colima start) and try again."; done_msg; exit 1; }
bash linux/ft_6_stop_dryrun.sh

done_msg
