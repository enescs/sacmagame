#!/usr/bin/env bash
# One-time setup: build a local venv and install pygame into it.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip --quiet
./.venv/bin/python -m pip install -r requirements.txt

cat <<'EOF'

done.

  play:  ./play.sh
  host:  ./host.sh

EOF
