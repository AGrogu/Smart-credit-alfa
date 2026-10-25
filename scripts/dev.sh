#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PIP_CACHE_DIR="$PWD/work/pip-cache"
export npm_config_cache="$PWD/work/npm-cache"
export SCARF_ANALYTICS=false DO_NOT_TRACK=1
mkdir -p work backend/data
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r backend/requirements.lock
(cd frontend && npm ci --no-audit --no-fund && npm run build)
exec ./scripts/run.sh
