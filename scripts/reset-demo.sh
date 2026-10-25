#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${1:-}" != "--confirm-delete-demo-data" ]; then
  echo 'Эта команда удаляет только локальную SQLite базу проекта, включая заявки и документы.'
  echo 'После отдельного разрешения на сброс: ./scripts/reset-demo.sh --confirm-delete-demo-data'
  exit 1
fi
if [ -n "${DATABASE_URL:-}" ]; then echo 'Сброс пользовательского DATABASE_URL запрещён этим скриптом.'; exit 1; fi
# Stop the local application before explicitly invoking this operation.
rm -f backend/data/credit.db backend/data/credit.db-shm backend/data/credit.db-wal
printf '%s\n' 'Локальная демо-база удалена. Данные будут созданы при следующем запуске.'
