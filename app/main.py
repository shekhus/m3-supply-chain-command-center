"""FastAPI entry point. Routers (run, approvals) are added as their weeks land."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

app = FastAPI(title="m3-supply-chain-command-center", version="0.1.0")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """The bare URL is not an endpoint: send people to the interactive docs instead of a 404."""
    return RedirectResponse("/docs")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
