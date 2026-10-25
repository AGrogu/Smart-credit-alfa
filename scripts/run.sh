#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backend/data
export DATABASE_URL="${DATABASE_URL:-sqlite:///$PWD/backend/data/credit.db}"
export PYTHONDONTWRITEBYTECODE=1
cd backend
exec ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${APP_PORT:-8080}"
