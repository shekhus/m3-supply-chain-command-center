"""One SQLAlchemy engine per database URL. The API's database endpoints need Postgres (D-009)."""

from __future__ import annotations

from functools import cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url

from app.config import Settings, get_settings


@cache
def _engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def get_engine(settings: Annotated[Settings, Depends(get_settings)]) -> Engine:
    url = settings.database_url
    if not url or make_url(url).get_backend_name() != "postgresql":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DATABASE_URL must point at Postgres")
    return _engine(url)
