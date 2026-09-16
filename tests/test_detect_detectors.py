"""Each detector against a series whose right answer can be worked out on paper.

The generated world tells us the detectors work on one world. These tell us *why* they work, and they are the
tests that fail when someone changes a constant and assumes nothing moved.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from synthetic import START, config_with, enriched, series, weekly

from app.config import REPO_ROOT
from detect import cusum, threshold
from detect.baseline import baseline, enrich
from detect.config import DetectConfig, Threshold, load_detect_config
from detect.models import Severity
from detect.series import Grain

POLICY = REPO_ROOT / "policy.yaml"


@pytest.fixture(scope="module")
def cfg() -> DetectConfig:
    return load_detect_config(POLICY)


# --- baseline ---------------------------------------------------------------------------------


def test_a_flat_series_with_one_drop_scores_the_drop_in_standard_deviations(cfg: DetectConfig) -> None:
    """28 days alternating 0.94/0.96 (mean 0.95, sd 0.01), then 0.90: 5 sigma before the policy floor."""
    values = [0.94 if i % 2 else 0.96 for i in range(28)] + [0.90]
    s = series(values, config_with(cfg, day_of_week_adjust=False))
    base = baseline(s, START + timedelta(days=28), "otif_rate", config_with(cfg, day_of_week_adjust=False))
    assert base is not None
    assert base.points == 28
    assert base.expected == pytest.approx(0.95, abs=1e-9)
    assert base.delta == pytest.approx(-0.05, abs=1e-9)
    assert base.std == pytest.approx(cfg.min_std["otif_rate"])   # the policy floor, above the raw 0.0101 sd
    assert base.z == pytest.approx(-0.05 / cfg.min_std["otif_rate"], rel=1e-6)


def test_the_policy_floor_stops_a_flat_series_manufacturing_an_enormous_z(cfg: DetectConfig) -> None:
    s = series([0.95] * 28 + [0.94], cfg)
    base = baseline(s, START + timedelta(days=28), "otif_rate", cfg)
    assert base is not None and base.std == cfg.min_std["otif_rate"]
    assert abs(base.z) == pytest.approx(0.01 / cfg.min_std["otif_rate"], rel=1e-6)  # finite, and below WARN
    assert abs(base.z) < cfg.z_warn


def test_an_ordinary_sunday_is_not_an_anomaly_when_the_week_has_a_shape(cfg: DetectConfig) -> None:
    """The point of the day-of-week adjustment: Sundays run 20 points below the week and that is normal."""
    pattern = [0.95, 0.95, 0.95, 0.95, 0.95, 0.90, 0.75]   # Monday … Sunday
    values = weekly(pattern, weeks=5)
    sunday = START + timedelta(days=(6 - START.weekday()) % 7 + 28)

    adjusted = baseline(series(values, cfg), sunday, "otif_rate", cfg)
    naive = baseline(series(values, config_with(cfg, day_of_week_adjust=False)), sunday, "otif_rate",
                     config_with(cfg, day_of_week_adjust=False))
    assert adjusted is not None and naive is not None
    assert adjusted.expected == pytest.approx(0.75, abs=1e-9)   # Sunday is measured against Sundays
    assert abs(adjusted.z) < cfg.z_warn                         # ... and is unremarkable
    assert abs(naive.z) > abs(adjusted.z)                       # ... where the naive baseline cries weekly


def test_holidays_are_excluded_from_the_baseline_so_a_shutdown_does_not_lower_the_bar(
        cfg: DetectConfig) -> None:
    """2026-07-03/04 are in the policy's holiday list; a collapse on them must not become the new normal."""
    start = date(2026, 6, 10)
    values = [0.95] * 28
    days = [start + timedelta(days=i) for i in range(28)]
    values = [0.20 if cfg.is_holiday(d) else v for d, v in zip(days, values, strict=True)]
    s = series(values, cfg, start=start)

    base = baseline(s, start + timedelta(days=27), "otif_rate", cfg)
    assert base is not None
    assert base.excluded_holidays == sum(1 for d in days[:-1] if cfg.is_holiday(d)) > 0
    assert base.expected == pytest.approx(0.95, abs=1e-9)   # the holiday collapse never entered the mean


def test_a_series_with_too_little_history_gets_no_opinion(cfg: DetectConfig) -> None:
    s = series([0.95] * 10 + [0.10], cfg)
    assert baseline(s, START + timedelta(days=10), "otif_rate", cfg) is None


def test_enrich_gives_every_day_the_same_baseline_it_would_get_alone(cfg: DetectConfig) -> None:
    values = weekly([0.95, 0.94, 0.96, 0.95, 0.93, 0.90, 0.80], weeks=6)
    s = enrich(series(values, cfg), "otif_rate", cfg)
    day = START + timedelta(days=35)
    alone = baseline(series(values, cfg), day, "otif_rate", cfg)
    i = s.index[day]
    assert alone is not None and s.z is not None and s.expected is not None
    assert float(s.z[i]) == pytest.approx(alone.z)
    assert float(s.expected[i]) == pytest.approx(alone.expected)


# --- threshold --------------------------------------------------------------------------------


def with_rule(cfg: DetectConfig, rule: Threshold, metric: str = "otif_rate") -> DetectConfig:
    """A config carrying one rule of our own.

    These tests are about the threshold *engine* — when a run starts, what breaks it, which line wins — not
    about the levels the business happens to have chosen this week. Asserting against the repo's policy made
    them fail the moment those levels were set from measured data (docs/decisions.md B-006), which is a test
    breaking for the wrong reason.
    """
    return config_with(cfg, thresholds={**cfg.thresholds, metric: rule}, overrides={})



def test_a_hard_rule_fires_only_after_its_consecutive_days(cfg: DetectConfig) -> None:
    """A line at 0.90 for 3 consecutive days: two bad days are a Tuesday, three are an incident."""
    cfg = with_rule(cfg, Threshold(warn=0.92, high=0.90, consecutive_days=3, direction="down"))
    values = [0.95] * 28 + [0.88, 0.88, 0.88]
    s = enriched(values, cfg)
    second, third = START + timedelta(days=29), START + timedelta(days=30)
    assert threshold.evaluate(s, second, "otif_rate", cfg) is None
    hit = threshold.evaluate(s, third, "otif_rate", cfg)
    assert hit is not None
    assert hit.severity is Severity.HIGH and hit.level == 0.90 and hit.days_in_breach == 3
    assert hit.first_breach == START + timedelta(days=28)


def test_a_missing_day_breaks_the_run_rather_than_bridging_it(cfg: DetectConfig) -> None:
    """A metric that was not published is not evidence of a breach on the day it is missing."""
    cfg = with_rule(cfg, Threshold(warn=0.92, high=0.90, consecutive_days=3, direction="down"))
    values = [0.95] * 28 + [0.88] * 5
    gap = START + timedelta(days=29)
    s = enriched(values, cfg, skip=[gap])
    # days 28, 30, 31, 32 are in breach; the run restarts after the gap instead of counting through it
    assert threshold.evaluate(s, START + timedelta(days=31), "otif_rate", cfg) is None   # only two days since
    hit = threshold.evaluate(s, START + timedelta(days=32), "otif_rate", cfg)
    assert hit is not None and hit.first_breach == START + timedelta(days=30)


def test_the_warn_line_fires_where_the_high_line_does_not(cfg: DetectConfig) -> None:
    cfg = with_rule(cfg, Threshold(warn=0.92, high=0.90, consecutive_days=3, direction="down"))
    s = enriched([0.95] * 28 + [0.915, 0.915, 0.915], cfg)
    hit = threshold.evaluate(s, START + timedelta(days=30), "otif_rate", cfg)
    assert hit is not None and hit.severity is Severity.WARN and hit.level == 0.92


def test_a_two_sided_rule_fires_on_a_positive_swing_too(cfg: DetectConfig) -> None:
    """Yield variance is bad in both directions: output far above standard is a measurement problem."""
    cfg = with_rule(cfg, Threshold(warn_abs=1.5, high_abs=3.0, consecutive_days=2, direction="both"),
                    "yield_variance_pct")
    grain = Grain("y", "daily_yield", ("plant",), "yield_variance_pct", "input_lb", "both")
    s = enriched([0.2] * 28 + [4.0, 4.0], cfg, metric="yield_variance_pct", grain=grain, volume=5000.0)
    hit = threshold.evaluate(s, START + timedelta(days=29), "yield_variance_pct", cfg)
    assert hit is not None and hit.severity is Severity.HIGH and hit.basis == "abs"


def test_a_z_based_rule_reads_the_segments_own_history(cfg: DetectConfig) -> None:
    """400 open lines is routine at one plant and alarming at another, so backlog's rule is in sigmas."""
    cfg = with_rule(cfg, Threshold(warn_z=2.0, high_z=3.0, consecutive_days=2, direction="up"),
                    "open_backlog_lines")
    grain = Grain("b", "daily_backlog_plant", ("plant",), "open_backlog_lines", "open_backlog_lines", "up")
    values = [300 + (i % 5) for i in range(28)] + [480, 480]
    s = enriched(values, cfg, metric="open_backlog_lines", grain=grain)
    hit = threshold.evaluate(s, START + timedelta(days=29), "open_backlog_lines", cfg)
    assert hit is not None and hit.basis == "z" and hit.severity is Severity.HIGH


def test_a_metric_with_no_rule_in_the_policy_produces_no_hit(cfg: DetectConfig) -> None:
    assert threshold.evaluate(enriched([0.95] * 30, cfg), START + timedelta(days=29), "made_up", cfg) is None


# --- cusum ------------------------------------------------------------------------------------


def test_cusum_catches_a_drift_too_small_for_the_z_detector(cfg: DetectConfig) -> None:
    """A one-sigma-ish step that never reaches z=2 on any single day, sustained for a fortnight."""
    values = [0.95, 0.96, 0.94, 0.95, 0.96, 0.94, 0.95] * 4 + [0.925] * 14
    s = enriched(values, cfg)
    day = START + timedelta(days=len(values) - 1)
    assert s.z is not None and max(abs(float(z)) for z in s.z[-14:]) < cfg.z_high

    hit = cusum.evaluate(s, day, cfg)
    assert hit is not None and hit.direction == "down"
    assert hit.change_point >= START + timedelta(days=26)   # the shift, not the start of the series
    assert hit.run_days >= 7


def test_cusum_stays_silent_on_a_series_that_only_wobbles(cfg: DetectConfig) -> None:
    values = [0.95, 0.96, 0.94, 0.95, 0.96, 0.94, 0.95] * 8
    s = enriched(values, cfg)
    assert cusum.evaluate(s, START + timedelta(days=len(values) - 1), cfg) is None


def test_cusum_ignores_holidays_so_a_shutdown_is_not_a_change_point(cfg: DetectConfig) -> None:
    """Without the holiday skip, the 4 July week alone pushes the accumulator over the decision interval."""
    start = date(2026, 6, 1)
    values = [0.95 if not cfg.is_holiday(start + timedelta(days=i)) else 0.30 for i in range(45)]
    s = enriched(values, cfg, start=start)
    assert any(cfg.is_holiday(d) for d in s.dates)
    assert cusum.evaluate(s, start + timedelta(days=44), cfg) is None


def test_no_detector_module_imports_an_llm() -> None:
    """Principle 1, enforced: nothing under detect/ may reach a model. Numbers are computed, not written."""
    banned = ("llm", "openai", "anthropic", "groq", "langchain", "langgraph")
    for path in sorted(Path(REPO_ROOT / "detect").glob("*.py")):
        source = path.read_text(encoding="utf-8").lower()
        imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        assert not [line for line in imports if any(term in line for term in banned)], path.name
