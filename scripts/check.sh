#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check backend tests
(cd frontend && npm run typecheck && npm run lint && npm run build)
