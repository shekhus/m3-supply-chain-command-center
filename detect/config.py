"""policy.yaml, typed. The detectors read this; they never carry a number of their own.

Principle 4: thresholds, the holiday calendar and the noise floors live in the policy file and are enforced in
code. A number that appears in a detector and not in the policy is a number nobody can change without a commit
from an engineer, which is the wrong person to own an operating threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from metrics.runner import MetricsError, load_policy


@dataclass(frozen=True)
class Threshold:
    """A hard rule for one metric: cross it for N days running and the answer is not a judgement call."""

    warn: float | None = None
    high: float | None = None
    warn_abs: float | None = None
    high_abs: float | None = None
    warn_z: float | None = None
    high_z: float | None = None
    warn_drop: float | None = None    # this far below (or above) the segment's own baseline, in its own units
    high_drop: float | None = None
    consecutive_days: int = 1
    window_days: int = 1      # compare a rolling volume-weighted mean, not one day's number
    direction: str = "down"


@dataclass(frozen=True)
class DetectConfig:
    baseline_days: int
    day_of_week_adjust: bool
    holidays: frozenset[date]
    holiday_window_days: int
    min_baseline_points: int
    z_warn: float
    z_high: float
    cusum_k: float
    cusum_h: float
    min_std: dict[str, float]
    min_volume: dict[str, float]
    min_magnitude: dict[str, float]
    holiday_max_severity: str
    statistical_max_severity: str
    thresholds: dict[str, Threshold]
    overrides: dict[str, dict[str, Threshold]]

    def threshold_for(self, metric: str, grain: str | None = None) -> Threshold | None:
        """The rule for this metric at this grain.

        A line drawn for the company is not the line for one lane: 88% OTIF for three days is a crisis across
        a plant and an ordinary week on a single customer's lane. Without this, a threshold written for the
        coarse grain fires all day long at the fine one, and the brief becomes noise.
        """
        if grain and grain in self.overrides and metric in self.overrides[grain]:
            return self.overrides[grain][metric]
        return self.thresholds.get(metric)

    def is_holiday(self, day: date) -> bool:
        """A holiday, or close enough that the business is running a holiday week (A5's whole point)."""
        span = self.holiday_window_days
        return any(abs((day - h).days) <= span for h in self.holidays)

    def baseline_window(self, as_of: date) -> tuple[date, date]:
        return as_of - timedelta(days=self.baseline_days), as_of - timedelta(days=1)

    def floor(self, mapping: dict[str, float], metric: str, default: float) -> float:
        return float(mapping.get(metric, mapping.get("default", default)))


def load_detect_config(policy_file: Path) -> DetectConfig:
    policy = load_policy(policy_file)
    if "detection" not in policy:
        raise MetricsError(f"{policy_file.name}: missing 'detection'")
    detection, seasonality = policy["detection"], policy["seasonality"]
    thresholds = {name: Threshold(**spec) for name, spec in policy["thresholds"].items()}
    overrides = {
        grain: {metric: Threshold(**{**policy["thresholds"].get(metric, {}), **spec})
                for metric, spec in by_metric.items()}
        for grain, by_metric in (policy.get("threshold_overrides") or {}).items()
    }
    return DetectConfig(
        baseline_days=int(seasonality["baseline_days"]),
        day_of_week_adjust=bool(seasonality["day_of_week_adjust"]),
        holidays=frozenset(date.fromisoformat(d) for d in seasonality["known_holidays"]),
        holiday_window_days=int(seasonality["holiday_window_days"]),
        min_baseline_points=int(detection["min_baseline_points"]),
        z_warn=float(detection["z"]["warn"]),
        z_high=float(detection["z"]["high"]),
        cusum_k=float(detection["cusum"]["k"]),
        cusum_h=float(detection["cusum"]["h"]),
        min_std={k: float(v) for k, v in detection["min_std"].items()},
        min_volume={k: float(v) for k, v in detection["min_volume"].items()},
        min_magnitude={k: float(v) for k, v in detection["min_magnitude"].items()},
        holiday_max_severity=str(detection["holiday_max_severity"]),
        statistical_max_severity=str(detection["statistical_max_severity"]),
        thresholds=thresholds,
        overrides=overrides,
    )
