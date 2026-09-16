"""The metric layer: one definition, computed in SQL, matching the generator's independent recomputation.

The SQL recomputes on-time and in-full from the raw fields rather than trusting gold's published flags, so a
disagreement between this project and the trusted data foundation would show up here rather than in a brief.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, create_engine, text

from app.config import REPO_ROOT, get_settings
from db.migrate import migrate
from metrics import runner

DATA = REPO_ROOT / "data"
POLICY = REPO_ROOT / "policy.yaml"
SERIES = ("daily_otif_total", "daily_otif_plant", "daily_otif_lane", "daily_fill_plant_group",
          "daily_backlog_plant", "daily_yield", "daily_inventory_location")
KEYS = {
    "daily_otif_total": ["metric_date"],
    "daily_otif_plant": ["metric_date", "plant"],
    "daily_otif_lane": ["metric_date", "plant", "customer_no"],
    "daily_fill_plant_group": ["metric_date", "plant", "product_group"],
    "daily_backlog_plant": ["metric_date", "plant"],
    "daily_yield": ["metric_date", "plant", "line", "product_group"],
    "daily_inventory_location": ["metric_date", "location"],
}


@pytest.fixture(scope="module")
def metrics_db(pg_admin: Engine) -> Iterator[Engine]:
    """One database with gold loaded and every metric computed: the loads are slow, the assertions are not."""
    if not (DATA / "gold" / "fact_delivery.parquet").exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    name = f"m3cc_test_{uuid.uuid4().hex[:12]}"
    with pg_admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_admin.url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        migrate(url)
        runner.run(engine, POLICY, DATA / "gold", "standalone")
        yield engine
    finally:
        engine.dispose()
        with pg_admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _sql(engine: Engine, table: str) -> pd.DataFrame:
    return pd.read_sql(f"SELECT * FROM metrics.{table}", engine).drop(columns=["computed_at"])


def _generated(name: str) -> pd.DataFrame:
    return pd.read_parquet(DATA / "metrics" / f"{name}.parquet")


# --- the definition ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_recomputed_flags_agree_with_the_published_gold_flags(metrics_db: Engine) -> None:
    disagreements = pd.read_sql(
        "SELECT count(*) AS n FROM metrics.v_delivery_flags f JOIN gold.fact_delivery d "
        "ON d.order_no = f.order_no AND d.line_no = f.line_no "
        "WHERE f.on_time <> d.on_time OR f.in_full <> d.in_full OR f.otif <> d.otif", metrics_db)
    assert int(disagreements["n"].iloc[0]) == 0


@pytest.mark.postgres
def test_the_answer_key_marker_never_reaches_the_database(metrics_db: Engine) -> None:
    columns = pd.read_sql(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'gold'",
        metrics_db)
    assert "_anomaly_id" not in set(columns["column_name"])


@pytest.mark.postgres
def test_policy_constants_are_loaded_from_the_policy_file(metrics_db: Engine) -> None:
    constants = dict(pd.read_sql("SELECT name, value FROM metrics.constants", metrics_db).to_numpy())
    policy = runner.load_policy(POLICY)
    assert constants == {k: float(v) for k, v in policy["metrics"].items()}
    assert constants["weight_tolerance"] == 0.02


# --- parity with the generator ----------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.parametrize("series", SERIES)
def test_sql_metrics_match_the_generators_own_recomputation(metrics_db: Engine, series: str) -> None:
    keys = KEYS[series]
    ours = _sql(metrics_db, series)
    theirs = _generated(series)
    for frame in (ours, theirs):
        frame["metric_date"] = pd.to_datetime(frame["metric_date"]).dt.date
    merged = ours.merge(theirs, on=keys, how="inner", suffixes=("_sql", "_gen"))
    # the SQL backlog series is continuous (a row per calendar day); the generator's covers shipping days only
    assert len(merged) == len(theirs)
    for column in [c for c in theirs.columns if c not in keys and f"{c}_sql" in merged]:
        left, right = merged[f"{column}_sql"].astype(float), merged[f"{column}_gen"].astype(float)
        assert left.sub(right).abs().max() < 1e-9, f"{series}.{column} differs"


@pytest.mark.postgres
def test_the_seeded_anomalies_are_visible_in_the_sql_series(metrics_db: Engine) -> None:
    """A1's lane and A2's group: the windows the answer key names must be measurably worse in SQL too."""
    lane = pd.read_sql(
        "SELECT metric_date, otif_rate FROM metrics.daily_otif_lane "
        "WHERE plant = 'PLT-02' AND customer_no = 'C000031'", metrics_db)
    lane["metric_date"] = pd.to_datetime(lane["metric_date"]).dt.date
    window = lane[(lane["metric_date"] >= date(2025, 9, 8)) & (lane["metric_date"] <= date(2025, 9, 28))]
    before = lane[(lane["metric_date"] >= date(2025, 7, 14)) & (lane["metric_date"] < date(2025, 9, 8))]
    assert window["otif_rate"].mean() < before["otif_rate"].mean() - 0.15

    group = pd.read_sql(
        "SELECT metric_date, fill_rate_weight, fill_rate_count FROM metrics.daily_fill_plant_group "
        "WHERE plant = 'PLT-01' AND product_group = 'CASE-READY'", metrics_db)
    group["metric_date"] = pd.to_datetime(group["metric_date"]).dt.date
    hit = group[(group["metric_date"] >= date(2025, 11, 10)) & (group["metric_date"] <= date(2025, 11, 23))]
    base = group[(group["metric_date"] >= date(2025, 9, 15)) & (group["metric_date"] < date(2025, 11, 10))]
    assert hit["fill_rate_weight"].mean() < base["fill_rate_weight"].mean() - 0.04
    assert hit["fill_rate_count"].mean() > hit["fill_rate_weight"].mean()  # invisible on the count basis


# --- reruns -----------------------------------------------------------------------------------


@pytest.mark.postgres
def test_recomputing_a_window_is_idempotent(metrics_db: Engine) -> None:
    before = {s: _sql(metrics_db, s) for s in SERIES}
    policy = runner.load_policy(POLICY)
    runner.run_sql(metrics_db, policy, date(2025, 9, 1), date(2025, 9, 30))
    for series, frame in before.items():
        after = _sql(metrics_db, series)
        assert len(after) == len(frame)
        pd.testing.assert_frame_equal(after.sort_values(KEYS[series]).reset_index(drop=True),
                                      frame.sort_values(KEYS[series]).reset_index(drop=True))


@pytest.mark.postgres
def test_a_run_records_its_window_and_row_counts(metrics_db: Engine) -> None:
    runs = pd.read_sql("SELECT * FROM metrics.runs ORDER BY run_id DESC LIMIT 1", metrics_db)
    row = runs.iloc[0]
    assert row["gold_source"] == "standalone" and row["error"] is None
    assert row["finished_at"] is not None and row["from_date"] < row["to_date"]
    assert row["tables"]["fact_delivery"] > 50000 and row["tables"]["020_daily_otif_total"] > 500


# --- guards -----------------------------------------------------------------------------------


def test_metric_sql_files_are_numbered_and_ordered() -> None:
    files = runner.discover()
    assert [p.name[:3] for p in files] == sorted(p.name[:3] for p in files)
    assert files[0].name.startswith("010_")  # the flags view comes first; every rate reads it


def test_a_badly_named_metric_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "flags.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(runner.MetricsError, match="must be named"):
        runner.discover(tmp_path)


def test_a_policy_missing_a_section_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("version: 1\nmetrics: {weight_tolerance: 0.02}\n", encoding="utf-8")
    with pytest.raises(runner.MetricsError, match="missing 'thresholds'"):
        runner.load_policy(path)


def test_the_repo_policy_carries_what_later_weeks_read() -> None:
    policy = runner.load_policy(POLICY)
    assert policy["thresholds"]["otif_rate"]["high"] == 0.78   # set from the measured distribution (B-006)
    assert policy["threshold_overrides"]["otif_lane"]["otif_rate"]["window_days"] == 7
    assert policy["seasonality"]["day_of_week_adjust"] is True
    assert "2026-07-03" in policy["seasonality"]["known_holidays"]  # the decoy's week
    assert set(policy["allowed_actions"]) >= {"otif_drop", "fill_rate_drop", "yield_variance"}
    assert policy["limits"]["max_actions_per_brief"] == 5
    assert get_settings().policy_file == POLICY
