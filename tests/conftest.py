from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.config import REPO_ROOT

DATA = REPO_ROOT / "data"
# This stack's Postgres is published on 5433 (Project A uses 5432); override with TEST_DATABASE_URL.
DEFAULT_PG_URL = "postgresql+psycopg://m3:m3@localhost:5433/m3cc"


def _server_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or DEFAULT_PG_URL
    return url if make_url(url).get_backend_name() == "postgresql" else DEFAULT_PG_URL


@pytest.fixture(scope="session")
def pg_admin() -> Iterator[Engine]:
    """AUTOCOMMIT engine on the Postgres server, probed once per session (one timeout when absent).

    Skips when Postgres is unreachable. Set REQUIRE_POSTGRES=1 (CI) to fail instead of skipping.
    """
    server = make_url(_server_url())
    where = server.render_as_string(hide_password=True)
    admin = create_engine(server, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        admin.dispose()
        if os.environ.get("REQUIRE_POSTGRES") == "1":
            pytest.fail(f"Postgres unreachable at {where}: {exc}")
        pytest.skip(f"Postgres unreachable at {where} (start: make up)")
    yield admin
    admin.dispose()


@pytest.fixture
def pg_url(pg_admin: Engine) -> Iterator[str]:
    """A fresh, empty Postgres database per test, dropped afterwards."""
    name = f"m3cc_test_{uuid.uuid4().hex[:12]}"
    with pg_admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield pg_admin.url.set(database=name).render_as_string(hide_password=False)
    finally:
        with pg_admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
