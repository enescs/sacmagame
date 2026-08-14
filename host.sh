#!/usr/bin/env bash
# Host a game. Everyone else just runs ./play.sh and picks it from the list.
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/python -m sacma.server "$@"
