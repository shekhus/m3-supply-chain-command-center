"""Apply numbered plain-SQL migrations from db/migrations to Postgres.

Postgres only (docs/decisions.md D-009): SQLite has no schemas, and production runs on Postgres.
Each migration runs in its own transaction together with its bookkeeping row, so a failing file
leaves no partial DDL and no record. A session advisory lock stops two runners racing.
Applied files are checksummed; editing one after it ran is an error — write a new migration instead.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# Arbitrary constant key for pg_advisory_lock; only this runner uses it.
LOCK_KEY = 7_310_0001

TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     integer PRIMARY KEY,
    name        text        NOT NULL,
    checksum    char(64)    NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(f"{path.name}: migration files must be named NNNN_snake_case.sql")
        version = int(match.group(1))
        if version in migrations:
            taken = migrations[version].name
            raise MigrationError(f"{path.name}: version {version:04d} already used by {taken}")
        # Normalise line endings so a CRLF checkout does not change the checksum.
        sql = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        migrations[version] = Migration(version, path.name, sql)
    return [migrations[v] for v in sorted(migrations)]


def require_postgres(url: str) -> None:
    if not url:
        raise MigrationError("DATABASE_URL is not set; migrations need Postgres (see .env.example)")
    backend = make_url(url).get_backend_name()
    if backend != "postgresql":
        raise MigrationError(f"migrations need Postgres, got '{backend}' (docs/decisions.md D-009)")


def _applied(conn: Connection) -> dict[int, tuple[str, str]]:
    rows = conn.execute(text("SELECT version, name, checksum FROM public.schema_migrations")).all()
    return {int(r.version): (str(r.name), str(r.checksum)) for r in rows}


def _check_history(migrations: list[Migration], applied: dict[int, tuple[str, str]]) -> list[Migration]:
    """Return pending migrations; raise if history on disk and in the database disagree."""
    on_disk = {m.version: m for m in migrations}
    for version, (name, checksum) in sorted(applied.items()):
        disk = on_disk.get(version)
        if disk is None:
            raise MigrationError(f"{name} was applied but is missing from {MIGRATIONS_DIR.name}/")
        if disk.checksum != checksum:
            raise MigrationError(f"{disk.name} changed after it was applied; add a new migration instead")
    pending = [m for m in migrations if m.version not in applied]
    latest = max(applied, default=0)
    for m in pending:
        if m.version < latest:
            raise MigrationError(f"{m.name} is older than the latest applied migration ({latest:04d})")
    return pending


def migrate(url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in order. Returns the names applied (empty when up to date)."""
    require_postgres(url)
    migrations = discover(directory)
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    applied_now: list[str] = []
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
            conn.commit()
            try:
                conn.exec_driver_sql(TRACKING_DDL)
                conn.commit()
                pending = _check_history(migrations, _applied(conn))
                conn.commit()
                for m in pending:
                    try:
                        # Begin explicitly: SQLAlchemy does not see a transaction opened by the raw
                        # driver call, so without this a rollback would be a no-op.
                        with conn.begin():
                            # Raw driver cursor: a migration file holds many statements and may contain '%'.
                            conn.connection.driver_connection.execute(m.sql)  # type: ignore[union-attr]
                            conn.execute(
                                text("INSERT INTO public.schema_migrations (version, name, checksum) "
                                     "VALUES (:v, :n, :c)"),
                                {"v": m.version, "n": m.name, "c": m.checksum},
                            )
                    except Exception as exc:
                        raise MigrationError(f"{m.name} failed and was rolled back: {exc}") from exc
                    applied_now.append(m.name)
            finally:
                conn.rollback()
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
                conn.commit()
    finally:
        engine.dispose()
    return applied_now
