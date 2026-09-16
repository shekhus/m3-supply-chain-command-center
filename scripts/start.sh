#!/bin/sh
# Container entrypoint (docs/RUNBOOK.md). Migrations must succeed before the API starts.
set -e
# One image, two services: APP_ROLE=console runs the Streamlit console instead of the API.
if [ "${APP_ROLE:-api}" = "console" ]; then
  exec sh scripts/start_console.sh
fi
python scripts/migrate.py
# An empty host binds every interface, IPv4 and IPv6: docker's port mapping, a platform's public edge and an
# IPv6 private network all reach the API. Set HOST to narrow it.
exec uvicorn app.main:app --host "${HOST:-}" --port "${PORT:-8000}"
