"""Detector 1: rolling baseline with day-of-week adjustment → z-score.

Two corrections make the difference between a detector and an alarm that cries every Monday:

- **Day of week.** Shipping volume and OTIF have a weekly shape. Comparing a Monday against a 28-day mean that
  includes four Sundays produces a signal about the calendar, not about the business. The baseline window is
  de-seasonalised — each weekday's own offset removed — before the standard deviation and the z-score.
- **Holidays.** Days in a holiday window are excluded from the baseline, so a shutdown does not quietly lower
  the bar for the fortnight that follows it. Whether a *flagged* day is itself a holiday is handled in
  `severity.py`, which caps it — that is the A5 decoy.

The z-score is deliberately dumb: mean and standard deviation, with a policy floor under the deviation so a
series that has been flat for a month cannot produce an enormous z on its first ordinary wobble.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from detect.config import DetectConfig
from detect.series import SegmentSeries


@dataclass(frozen=True)
class Baseline:
    expected: float          # what this weekday normally looks like
    std: float
    points: int
    value: float
    delta: float
    z: float
    dow_offset: float
    excluded_holidays: int


def enrich(series: SegmentSeries, metric: str, cfg: DetectConfig) -> SegmentSeries:
    """Attach expected / std / delta / z to every day of the series, once, for every detector to read."""
    n = len(series)
    expected, std, delta, z = (np.full(n, np.nan) for _ in range(4))
    points = np.zeros(n, dtype=np.int64)
    valid = np.zeros(n, dtype=bool)
    for i in range(n):
        base = _compute(series, i, metric, cfg)
        if base is None:
            continue
        expected[i], std[i], delta[i], z[i] = base.expected, base.std, base.delta, base.z
        points[i], valid[i] = base.points, True
    series.expected, series.std, series.delta, series.z = expected, std, delta, z
    series.points, series.valid = points, valid
    return series


def baseline(series: SegmentSeries, day: date, metric: str, cfg: DetectConfig) -> Baseline | None:
    """The baseline for one day, or None when there is too little clean history to have an opinion."""
    i = series.index.get(day)
    return None if i is None else _compute(series, i, metric, cfg)


def _compute(series: SegmentSeries, i: int, metric: str, cfg: DetectConfig) -> Baseline | None:
    window = series.window(i, cfg.baseline_days)
    keep = ~series.holiday[window]
    values = series.values[window][keep]
    if len(values) < cfg.min_baseline_points:
        return None

    weekday = series.weekday[window][keep]
    mean = float(values.mean())
    offsets = _dow_offsets(values, weekday, mean) if cfg.day_of_week_adjust else np.zeros(7)
    adjusted = values - offsets[weekday]
    std = float(adjusted.std(ddof=1)) if len(adjusted) > 1 else 0.0
    std = max(std, cfg.floor(cfg.min_std, metric, 0.0))

    offset = float(offsets[series.weekday[i]])
    value = float(series.values[i])
    expected = mean + offset
    delta = value - expected
    return Baseline(expected=expected, std=std, points=len(values), value=value, delta=delta,
                    z=delta / std if std > 0 else 0.0, dow_offset=offset,
                    excluded_holidays=int((~keep).sum()))


def _dow_offsets(values: np.ndarray, weekday: np.ndarray, mean: float) -> np.ndarray:
    """How much each weekday usually sits above or below the mean. Weekdays seen once are left at zero."""
    counts = np.bincount(weekday, minlength=7)
    sums = np.bincount(weekday, weights=values, minlength=7)
    with np.errstate(invalid="ignore"):
        means = np.where(counts > 0, sums / np.maximum(counts, 1), mean)
    return np.where(counts >= 2, means - mean, 0.0)
