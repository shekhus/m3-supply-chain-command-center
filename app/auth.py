"""Server-side roles from the X-API-Key header (CLAUDE.md principle 7). The UI never decides permissions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings

Role = Literal["viewer", "analyst", "owner"]
ROLES: tuple[Role, ...] = ("viewer", "analyst", "owner")


@dataclass(frozen=True)
class Principal:
    """Who is asking, and which brief they are entitled to.

    `audience` comes from the key, never from the request. A request that could name its own audience would
    make the permission filter in `pack/permissions.py` decorative — the filter is only worth having if the
    caller cannot choose what it filters to.
    """

    role: Role
    audience: str = "vp"


def current_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    key = x_api_key or ""
    role = settings.api_keys.get(key)
    if role not in ROLES:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or unknown X-API-Key")
    return Principal(role=role, audience=settings.api_audiences.get(key, "vp"))  # type: ignore[arg-type]


def require(*roles: Role) -> Callable[[Principal], Principal]:
    def check(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"role '{principal.role}' cannot do this; requires {' or '.join(roles)}")
        return principal

    return check
