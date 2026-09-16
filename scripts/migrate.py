"""Apply db/migrations to DATABASE_URL. Equivalent of `make migrate`."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings  # noqa: E402
from db.migrate import MigrationError, migrate  # noqa: E402


def main() -> int:
    try:
        applied = migrate(get_settings().database_url)
    except MigrationError as exc:
        print(f"migrate: {exc}", file=sys.stderr)
        return 1
    for name in applied:
        print(f"applied {name}")
    if not applied:
        print("database is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
