"""Runtime settings, read from the environment only (CLAUDE.md: secrets in env).

Two data sources are possible (plan B14): standalone, where `synth/` generates gold with seeded anomalies into
`data/gold`, and integrated, where `GOLD_SOURCE=external` reads gold produced by m3-trusted-data-foundation.
The rest of the system does not care which: it reads `gold.*` in Postgres either way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(env_file: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env loader so local runs need no extra dependency.

    The repo's .env wins over variables already in the shell: a developer machine often exports an unrelated
    key for another project. Containers and CI never see a .env (.dockerignore, .gitignore), so there the real
    environment is the only source.
    """
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip().upper()] = value.strip().strip("'\"")


_load_dotenv()


def normalise_database_url(url: str) -> str:
    """Hosted Postgres (Railway, Heroku-style) hands out `postgres://` or `postgresql://` URLs, for which
    SQLAlchemy would pick psycopg2 (not installed). Use psycopg 3 unless a driver is already named."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _parse_api_keys(raw: str) -> dict[str, str]:
    """"role:key,role:key" → {key: role}. Roles are checked server-side (app/auth.py)."""
    keys: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        role, key = pair.split(":", 1)
        if key.strip():
            keys[key.strip()] = role.strip()
    return keys


def _parse_audiences(raw: str) -> dict[str, str]:
    """"key:audience,key:audience" → {key: audience}.

    Which brief a request may see is decided by the key it presents, never by a parameter. A plant manager's
    key returns a plant manager's brief whatever the URL asks for — the alternative is an audience field that
    anybody can edit, which would make the permission filter decorative.
    """
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        key, audience = pair.split(":", 1)
        if key.strip() and audience.strip():
            out[key.strip()] = audience.strip()
    return out


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / os.environ.get("DATA_DIR", "data"))
    database_url: str = field(
        default_factory=lambda: normalise_database_url(os.environ.get("DATABASE_URL", "")))
    api_keys: dict[str, str] = field(default_factory=lambda: _parse_api_keys(os.environ.get("API_KEYS", "")))
    api_audiences: dict[str, str] = field(
        default_factory=lambda: _parse_audiences(os.environ.get("API_AUDIENCES", "")))
    policy_file: Path = field(
        default_factory=lambda: REPO_ROOT / os.environ.get("POLICY_FILE", "policy.yaml"))
    gold_source: str = field(default_factory=lambda: os.environ.get("GOLD_SOURCE", "standalone"))
    # Where the daily metrics are read from: "postgres" (a deployment that has run `make metrics`) or
    # "parquet" (the standalone demo, and the tests, reading what the generator wrote).
    metrics_source: str = field(default_factory=lambda: os.environ.get("METRICS_SOURCE", "postgres"))
    llm_provider: str = field(default_factory=lambda: os.environ.get("LLM_PROVIDER", "none"))
    llm_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", "claude-sonnet-5"))
    llm_price_in: float = field(default_factory=lambda: float(os.environ.get("LLM_PRICE_IN_PER_MTOK", "0")))
    llm_price_out: float = field(default_factory=lambda: float(os.environ.get("LLM_PRICE_OUT_PER_MTOK", "0")))
    jira_mode: str = field(default_factory=lambda: os.environ.get("JIRA_MODE", "mock"))
    console_url: str = field(default_factory=lambda: os.environ.get("CONSOLE_URL", "http://localhost:8501"))

    @property
    def gold_dir(self) -> Path:
        return self.data_dir / "gold"

    @property
    def ground_truth_dir(self) -> Path:
        return self.data_dir / "ground_truth"


def get_settings() -> Settings:
    return Settings()
