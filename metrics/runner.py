"""Load gold, then run metrics/sql in order (plan B1).

Two jobs, kept apart:

- `load_gold` copies the generated parquet tables into `gold.*` (standalone mode). It drops the generator's
  `_anomaly_id` marker: that column is answer-key material, and a detector able to read it would be scoring
  itself. With `GOLD_SOURCE=external` nothing is loaded — gold already comes from m3-trusted-data-foundation.
- `run_sql` executes the numbered files against Postgres, passing the constants policy.yaml defines (the
  weight tolerance, the at-risk window) and the date range, so one file recomputes a day or the whole history.

Every file merges by its own key, so a rerun for a date is idempotent and never duplicates a row.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import Engine, text

SQL_DIR = Path(__file__).resolve().parent / "sql"
FILENAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
GOLD_TABLES = ("dim_customer", "dim_item", "fact_delivery", "fact_inventory", "fact_yield")
GENERATOR_ONLY = ("_anomaly_id",)  # the seeded-anomaly marker never reaches the database the detectors read


class MetricsError(RuntimeError):
    pass


@dataclass
class MetricsResult:
    run_id: int | None
    gold_rows: dict[str, int] = field(default_factory=dict)
    metric_rows: dict[str, int] = field(default_factory=dict)
    from_date: date | None = None
    to_date: date | None = None


def discover(directory: Path = SQL_DIR) -> list[Path]:
    files = sorted(p for p in directory.glob("*.sql"))
    for path in files:
        if not FILENAME_RE.match(path.name):
            raise MetricsError(f"{path.name}: metric SQL must be named NNN_snake_case.sql")
    if not files:
        raise MetricsError(f"no metric SQL in {directory}")
    return files


def load_policy(path: Path) -> dict:
    policy = yaml.safe_load(path.read_text(encoding="utf-8"))
    for section in ("metrics", "thresholds", "seasonality", "allowed_actions", "limits"):
        if section not in policy:
            raise MetricsError(f"{path.name}: missing '{section}'")
    return policy


def load_gold(engine: Engine, gold_dir: Path) -> dict[str, int]:
    """Replace gold.* from the generated parquet. Standalone mode only."""
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        for name in reversed(GOLD_TABLES):
            conn.execute(text(f"TRUNCATE gold.{name} CASCADE"))
        for name in GOLD_TABLES:
            path = gold_dir / f"{name}.parquet"
            if not path.exists():
                raise MetricsError(f"{path} is missing; run `make synth-gold`")
            frame = pd.read_parquet(path).drop(columns=list(GENERATOR_ONLY), errors="ignore")
            columns = [c["name"] for c in _columns(conn, name)]
            missing = [c for c in columns if c not in frame.columns]
            if missing:
                raise MetricsError(f"gold.{name}: parquet is missing {missing}")
            rows = frame[columns].astype(object).where(frame[columns].notna(), None)
            conn.execute(
                text(f"INSERT INTO gold.{name} ({', '.join(columns)}) "
                     f"VALUES ({', '.join(':' + c for c in columns)})"),
                [dict(zip(columns, row, strict=True)) for row in rows.itertuples(index=False, name=None)])
            counts[name] = len(frame)
    return counts


def _columns(conn, table: str) -> list[dict]:  # noqa: ANN001 - SQLAlchemy Connection
    rows = conn.execute(text(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_schema = 'gold' AND table_name = :t ORDER BY ordinal_position"), {"t": table}).mappings()
    return [dict(r) for r in rows]


def date_range(engine: Engine) -> tuple[date, date]:
    """The window gold actually covers: metrics never claim a day gold does not have."""
    with engine.connect() as conn:
        bounds = conn.execute(text(
            "SELECT least((SELECT min(issue_date) FROM gold.fact_delivery), "
            "             (SELECT min(run_date) FROM gold.fact_yield), "
            "             (SELECT min(snapshot_date) FROM gold.fact_inventory)) AS from_date, "
            "       greatest((SELECT max(issue_date) FROM gold.fact_delivery), "
            "                (SELECT max(run_date) FROM gold.fact_yield), "
            "                (SELECT max(snapshot_date) FROM gold.fact_inventory)) AS to_date")).one()
    if bounds.from_date is None or bounds.to_date is None:
        raise MetricsError("gold is empty; load it first (`make metrics` in standalone mode does this)")
    return bounds.from_date, bounds.to_date


def load_constants(engine: Engine, policy: dict) -> dict[str, float]:
    """policy.yaml metrics.* → metrics.constants, so SQL reads the policy instead of hard-coded numbers."""
    constants = {k: float(v) for k, v in policy["metrics"].items()}
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO metrics.constants (name, value) VALUES (:name, :value) "
            "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, loaded_at = now()"),
            [{"name": k, "value": v} for k, v in constants.items()])
    return constants


def run_sql(engine: Engine, policy: dict, from_date: date, to_date: date,
            directory: Path = SQL_DIR) -> dict[str, int]:
    load_constants(engine, policy)
    params = {"from_date": from_date, "to_date": to_date}
    counts: dict[str, int] = {}
    for path in discover(directory):
        sql = path.read_text(encoding="utf-8")
        with engine.begin() as conn:
            conn.execute(text(sql), {k: v for k, v in params.items() if f":{k}" in sql})
        counts[path.stem] = _affected(engine, path.stem)
    return counts


def _affected(engine: Engine, stem: str) -> int:
    """Rows now in the table a file maintains (its name after the number), or 0 for a view."""
    table = stem.split("_", 1)[1]
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'metrics' AND table_name = :t"),
            {"t": table}).first()
        if exists is None:
            return 0
        return int(conn.execute(text(f"SELECT count(*) FROM metrics.{table}")).scalar_one())


def run(engine: Engine, policy_file: Path, gold_dir: Path, gold_source: str = "standalone",
        from_date: date | None = None, to_date: date | None = None) -> MetricsResult:
    policy = load_policy(policy_file)
    result = MetricsResult(run_id=None)
    with engine.begin() as conn:
        result.run_id = int(conn.execute(text(
            "INSERT INTO metrics.runs (gold_source) VALUES (:s) RETURNING run_id"),
            {"s": gold_source}).scalar_one())
    try:
        if gold_source == "standalone":
            result.gold_rows = load_gold(engine, gold_dir)
        covered_from, covered_to = date_range(engine)
        result.from_date = from_date or covered_from
        result.to_date = to_date or covered_to
        result.metric_rows = run_sql(engine, policy, result.from_date, result.to_date)
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(text("UPDATE metrics.runs SET finished_at = now(), error = :e WHERE run_id = :r"),
                         {"e": f"{type(exc).__name__}: {exc}"[:2000], "r": result.run_id})
        raise
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE metrics.runs SET finished_at = now(), from_date = :f, to_date = :t, "
            "tables = CAST(:tables AS jsonb) WHERE run_id = :r"),
            {"f": result.from_date, "t": result.to_date, "r": result.run_id,
             "tables": json.dumps({**result.gold_rows, **result.metric_rows})})
    return result
