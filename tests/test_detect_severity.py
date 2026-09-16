"""The gates that decide what becomes an incident — including the one the A5 decoy exists to test."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.config import REPO_ROOT
from detect import severity
from detect.baseline import Baseline
from detect.config import DetectConfig, load_detect_config
from detect.cusum import CusumHit
from detect.models import Anomaly, Severity
from detect.series import Grain
from detect.threshold import ThresholdHit

POLICY = REPO_ROOT / "policy.yaml"
GRAIN = Grain("otif_plant", "daily_otif_plant", ("plant",), "otif_rate", "lines", "down")
SEGMENT = {"plant": "PLT-01"}
ORDINARY_DAY = date(2026, 3, 10)
HOLIDAY = date(2026, 7, 4)


@pytest.fixture(scope="module")
def cfg() -> DetectConfig:
    return load_detect_config(POLICY)


def _base(value: float = 0.70, expected: float = 0.95, z: float = -6.0) -> Baseline:
    return Baseline(expected=expected, std=0.02, points=28, value=value, delta=value - expected, z=z,
                    dow_offset=0.0, excluded_holidays=0)


def _hit(sev: Severity = Severity.HIGH) -> ThresholdHit:
    return ThresholdHit(severity=sev, level=0.90, days_in_breach=3, basis="level", first_breach=ORDINARY_DAY)


def _shift(sev: Severity = Severity.HIGH) -> CusumHit:
    return CusumHit(severity=sev, statistic=11.0, direction="down", change_point=ORDINARY_DAY, run_days=6)


def _combine(cfg: DetectConfig, day: date = ORDINARY_DAY, value: float = 0.70, volume: float = 300.0,
             base: Baseline | None = None, hit: ThresholdHit | None = None,
             shift: CusumHit | None = None) -> Anomaly | None:
    return severity.combine(GRAIN, SEGMENT, day, value, volume, base if base else _base(value), hit, shift,
                            cfg, volume_scale=250.0)


def test_no_detector_means_no_anomaly(cfg: DetectConfig) -> None:
    quiet = Baseline(expected=0.95, std=0.02, points=28, value=0.949, delta=-0.001, z=-0.05,
                     dow_offset=0.0, excluded_holidays=0)
    assert _combine(cfg, base=quiet, value=0.949) is None


def test_severity_is_the_strongest_thing_any_detector_said(cfg: DetectConfig) -> None:
    anomaly = _combine(cfg, hit=_hit(Severity.HIGH), shift=_shift(Severity.WARN))
    assert anomaly is not None and anomaly.severity is Severity.HIGH
    assert {s.detector for s in anomaly.detectors} == {"baseline", "threshold", "cusum"}


def test_only_a_policy_breach_makes_an_incident(cfg: DetectConfig) -> None:
    """Statistics say a thing changed; policy says it matters. Both detectors agreeing is still only a WARN.

    Measured in the replay: without this rule the statistical detectors produced 322 of 365 HIGH flags, nearly
    all of them real movements nobody needed waking for (docs/decisions.md B-006).
    """
    alone = _combine(cfg)
    assert alone is not None and alone.severity is Severity.WARN
    assert alone.suppressed_reason is not None and "policy threshold" in alone.suppressed_reason

    both_statistical = _combine(cfg, shift=_shift(Severity.HIGH))
    assert both_statistical is not None and both_statistical.severity is Severity.WARN

    warned_by_policy = _combine(cfg, hit=_hit(Severity.WARN), shift=_shift(Severity.HIGH))
    assert warned_by_policy is not None and warned_by_policy.severity is Severity.WARN

    breached = _combine(cfg, hit=_hit(Severity.HIGH))
    assert breached is not None and breached.severity is Severity.HIGH
    assert breached.suppressed_reason is None


def test_a_holiday_dip_is_reported_and_capped_below_incident(cfg: DetectConfig) -> None:
    """A5: the dip is real, the plants were shut, and the key counts a HIGH here as a false positive."""
    anomaly = _combine(cfg, day=HOLIDAY, hit=_hit(), shift=_shift())
    assert anomaly is not None
    assert anomaly.severity is Severity.WARN            # capped ...
    assert anomaly.value == 0.70 and anomaly.delta is not None   # ... not deleted
    assert anomaly.suppressed_reason is not None and "holiday" in anomaly.suppressed_reason


def test_the_holiday_cap_reaches_the_days_around_the_holiday(cfg: DetectConfig) -> None:
    """A shutdown is a week, not a day; policy's holiday_window_days says how far the allowance stretches."""
    eve = HOLIDAY - timedelta(days=cfg.holiday_window_days)
    after = HOLIDAY + timedelta(days=cfg.holiday_window_days + 1)
    capped = _combine(cfg, day=eve, hit=_hit(), shift=_shift())
    beyond = _combine(cfg, day=after, hit=_hit(), shift=_shift())
    assert capped is not None and capped.severity is Severity.WARN
    assert beyond is not None and beyond.severity is Severity.HIGH


def test_a_rate_computed_from_a_handful_of_lines_is_not_an_incident(cfg: DetectConfig) -> None:
    thin = _combine(cfg, volume=3.0, hit=_hit(), shift=_shift())
    assert thin is not None and thin.severity is Severity.INFO
    assert thin.suppressed_reason is not None and "below the floor" in thin.suppressed_reason


def test_a_statistically_extreme_but_immaterial_move_is_not_an_incident(cfg: DetectConfig) -> None:
    """A 1-point move on a series that never moves is a 6-sigma event and nobody's morning."""
    tiny = Baseline(expected=0.99, std=0.002, points=28, value=0.98, delta=-0.01, z=-5.0,
                    dow_offset=0.0, excluded_holidays=0)
    anomaly = _combine(cfg, value=0.98, base=tiny, hit=_hit(), shift=_shift())
    assert anomaly is not None and anomaly.severity is Severity.INFO
    assert anomaly.suppressed_reason is not None and "material" in anomaly.suppressed_reason


def test_rank_puts_the_bigger_segment_first_when_the_movement_matches(cfg: DetectConfig) -> None:
    big = severity.combine(GRAIN, SEGMENT, ORDINARY_DAY, 0.70, 900.0, _base(), _hit(), None, cfg, 250.0)
    small = severity.combine(GRAIN, SEGMENT, ORDINARY_DAY, 0.70, 60.0, _base(), _hit(), None, cfg, 250.0)
    assert big is not None and small is not None and big.rank > small.rank


def test_rank_is_comparable_across_metrics_with_different_units(cfg: DetectConfig) -> None:
    """Pounds must not outrank percentage points just for being a larger number."""
    at_risk = Grain("inv", "daily_inventory_location", ("location",), "lb_at_risk_within_5d", "on_hand_lb",
                    "up")
    pounds = severity.combine(
        at_risk, {"location": "DC-EAST"}, ORDINARY_DAY, 3_000.0, 120_000.0,
        Baseline(expected=1_000.0, std=300.0, points=28, value=3_000.0, delta=2_000.0, z=6.0,
                 dow_offset=0.0, excluded_holidays=0), _hit(), None, cfg, volume_scale=120_000.0)
    points = severity.combine(GRAIN, SEGMENT, ORDINARY_DAY, 0.70, 250.0, _base(), _hit(), None, cfg, 250.0)
    assert pounds is not None and points is not None
    # 2,000 lb is four times the 500 lb policy calls material; a 25-point OTIF drop is five times its own.
    assert pounds.relative_magnitude == pytest.approx(4.0)
    assert points.relative_magnitude == pytest.approx(5.0)
    assert points.rank > pounds.rank


def test_an_anomaly_carries_a_resolvable_evidence_reference(cfg: DetectConfig) -> None:
    anomaly = _combine(cfg, hit=_hit())
    assert anomaly is not None
    assert anomaly.evidence_refs == [f"daily_otif_plant:plant=PLT-01:{ORDINARY_DAY.isoformat()}:otif_rate"]
    assert anomaly.to_dict()["severity"] == "HIGH"
    assert anomaly.window_start == ORDINARY_DAY
