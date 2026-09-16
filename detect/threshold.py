"""Detector 2: the hard rules from policy.yaml.

The baseline detector asks "is this unusual for you?". This one asks "is this acceptable at all?" — a lane
that has always run at 88% OTIF is not unusual, and is still below the line the business drew. The two
disagree often, and that is why both run.

A rule fires only after `consecutive_days` days on the wrong side of the line, so one bad Tuesday is not an
incident. Three shapes of rule, all read from the policy:

- `warn` / `high`       — an absolute level, compared in the metric's bad direction
- `warn_abs`/`high_abs` — magnitude regardless of sign (yield variance is bad in both directions)
- `warn_z` / `high_z`   — a level that only makes sense against the segment's own history (backlog counts,
  where 400 open lines is routine at one plant and alarming at another)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from detect.config import DetectConfig, Threshold
from detect.models import Severity
from detect.series import SegmentSeries


@dataclass(frozen=True)
class ThresholdHit:
    severity: Severity
    level: float
    days_in_breach: int
    basis: str               # "level", "abs" or "z"
    first_breach: date


def evaluate(series: SegmentSeries, day: date, metric: str, cfg: DetectConfig) -> ThresholdHit | None:
    rule = cfg.thresholds.get(metric)
    i = series.index.get(day)
    if rule is None or i is None:
        return None
    for severity, basis, limit in _rules(rule):
        if limit is None or not _breached(series, i, limit, basis, rule.direction):
            continue
        days, first = _consecutive(series, i, limit, basis, rule)
        if days >= rule.consecutive_days:
            return ThresholdHit(severity=severity, level=limit, days_in_breach=days, basis=basis,
                                first_breach=first)
    return None


def _rules(rule: Threshold) -> list[tuple[Severity, str, float | None]]:
    """HIGH first: a value past the high line is past the warn line too, and the stronger verdict wins."""
    return [(Severity.HIGH, "level", rule.high), (Severity.HIGH, "abs", rule.high_abs),
            (Severity.HIGH, "z", rule.high_z), (Severity.WARN, "level", rule.warn),
            (Severity.WARN, "abs", rule.warn_abs), (Severity.WARN, "z", rule.warn_z)]


def _breached(series: SegmentSeries, i: int, limit: float, basis: str, direction: str) -> bool:
    value = float(series.values[i])
    if basis == "abs":
        return abs(value) >= limit
    if basis == "z":
        if series.z is None or series.valid is None or not bool(series.valid[i]) or np.isnan(series.z[i]):
            return False
        z = float(series.z[i])
        return z >= limit if direction == "up" else -z >= limit
    if direction == "up":
        return value >= limit
    if direction == "both":
        return abs(value) >= limit
    return value <= limit


def _consecutive(series: SegmentSeries, i: int, limit: float, basis: str,
                 rule: Threshold) -> tuple[int, date]:
    """How many days running this has been in breach, counting back from day `i`.

    A day with no row breaks the run: a metric that was not published is not evidence of a breach.
    """
    limit_days = max(rule.consecutive_days * 4, 8)  # enough to answer the question; the rest is replay cost
    days, first, cursor = 0, series.dates[i], i
    while cursor >= 0 and days < limit_days:
        if cursor < i and series.dates[cursor] != series.dates[cursor + 1] - timedelta(days=1):
            break
        if not _breached(series, cursor, limit, basis, rule.direction):
            break
        days, first = days + 1, series.dates[cursor]
        cursor -= 1
    return days, first
