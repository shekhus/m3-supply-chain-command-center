"""FastAPI entry point.

The API is deliberately small: build a brief, read a brief, decide on one. Everything else — the metrics, the
detectors, the narration, the policy gate — is reached through `service.py`, so the HTTP layer has no opinions
of its own to get wrong.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.routers import brief

app = FastAPI(title="m3-supply-chain-command-center", version="0.1.0")
app.include_router(brief.router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """The bare URL is not an endpoint: send people to the interactive docs instead of a 404."""
    return RedirectResponse("/docs")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
