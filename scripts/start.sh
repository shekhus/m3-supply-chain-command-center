#!/bin/sh
# Container entrypoint (docs/RUNBOOK.md). Migrations must succeed before the API starts.
set -e
# One image, three roles. APP_ROLE=console runs the Streamlit console; APP_ROLE=cron runs one morning and
# exits, which is what a scheduler wants — a cron service that stays up is a cron service that ran once.
if [ "${APP_ROLE:-api}" = "console" ]; then
  exec sh scripts/start_console.sh
fi
if [ "${APP_ROLE:-api}" = "cron" ]; then
  python scripts/migrate.py
  exec python scripts/morning.py
fi
python scripts/migrate.py
# An empty host binds every interface, IPv4 and IPv6: docker's port mapping, a platform's public edge and an
# IPv6 private network all reach the API. Set HOST to narrow it.
exec uvicorn app.main:app --host "${HOST:-}" --port "${PORT:-8000}"
