#!/bin/sh
# Render (and `docker run -v`) mount the data disk owned by root. Fix ownership, then run as the
# unprivileged "app" user. If the disk cannot be made writable for that user, fall back to root
# (with a warning) rather than failing to start.
set -e
DATA="${COIN_AUDIT_DATA_DIR:-/data}"
mkdir -p "$DATA"
if [ "$(id -u)" = "0" ]; then
    find "$DATA" -xdev ! -user app -exec chown app:app {} + 2>/dev/null || true
    if gosu app sh -c "touch '$DATA/.write-test' && rm -f '$DATA/.write-test'" 2>/dev/null; then
        exec gosu app "$@"
    fi
    echo "WARNING: $DATA is not writable by the unprivileged user; running as root instead." >&2
fi
exec "$@"
