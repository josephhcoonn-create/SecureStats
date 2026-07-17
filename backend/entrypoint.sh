#!/bin/sh
# SecureStats backend entrypoint — one image, two roles.
#
#   SERVICE_ROLE=web    (default) → run Alembic migrations, then uvicorn.
#   SERVICE_ROLE=worker           → run the APScheduler worker only.
#
# Migrations run in the web role only, so a worker starting alongside the
# web service never races a second `alembic upgrade`. The worker depends on
# the web service being healthy (see docker-compose), which guarantees the
# schema is already migrated before any job could fire.
set -e

if [ "${SERVICE_ROLE:-web}" = "worker" ]; then
  echo "[entrypoint] SERVICE_ROLE=worker — starting scheduler worker"
  exec python -m app.worker
fi

echo "[entrypoint] Running Alembic migrations…"
alembic upgrade head

echo "[entrypoint] Starting uvicorn on 0.0.0.0:8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 "$@"
