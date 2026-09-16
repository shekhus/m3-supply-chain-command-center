from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from db.migrate import MIGRATIONS_DIR, MigrationError, discover, migrate, require_postgres

SCHEMAS = {"gold", "metrics", "ops"}


def _copy_migrations(tmp_path: Path) -> Path:
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, target)
    return target


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:
    migrate(pg_url)
    eng = create_engine(pg_url)
    yield eng
    eng.dispose()


# --- no database needed -------------------------------------------------------


def test_repo_migrations_are_well_formed() -> None:
    migrations = discover()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert all(m.name.endswith(".sql") and m.sql.strip() and m.checksum for m in migrations)


def test_bad_filename_rejected(tmp_path: Path) -> None:
    directory = _copy_migrations(tmp_path)
    (directory / "nope.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(MigrationError, match="must be named"):
        discover(directory)


@pytest.mark.parametrize("url", ["", "sqlite:///x.db"])
def test_non_postgres_url_rejected(url: str) -> None:
    with pytest.raises(MigrationError):
        require_postgres(url)


# --- Postgres -----------------------------------------------------------------


@pytest.mark.postgres
def test_fresh_database_gets_the_schemas(engine: Engine) -> None:
    with engine.connect() as conn:
        schemas = set(conn.execute(text("SELECT schema_name FROM information_schema.schemata")).scalars())
        recorded = conn.execute(
            text("SELECT name FROM public.schema_migrations ORDER BY version")).scalars().all()
    assert SCHEMAS <= schemas
    assert recorded == [m.name for m in discover()]


@pytest.mark.postgres
def test_rerun_is_a_no_op(engine: Engine, pg_url: str) -> None:
    assert migrate(pg_url) == []


@pytest.mark.postgres
def test_edited_applied_migration_is_refused(pg_url: str, tmp_path: Path) -> None:
    directory = _copy_migrations(tmp_path)
    migrate(pg_url, directory)
    first = sorted(directory.glob("*.sql"))[0]
    first.write_text(first.read_text(encoding="utf-8") + "\n-- edited after it ran\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(pg_url, directory)
