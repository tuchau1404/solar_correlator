#!/bin/sh
set -eu
# Invoke separately as: sync_and_cleanup.sh sync --config /absolute/config.json
# or: sync_and_cleanup.sh cleanup --config /absolute/config.json
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/storage_maintenance.py" "$@"
