#!/usr/bin/env bash
# Join a game. With no arguments it scans the LAN and shows what it finds.
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/python -m sacma.client "$@"
