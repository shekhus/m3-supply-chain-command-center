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
- `warn_drop`/`high_drop` — how far below its own baseline a segment has fallen, in the metric's own units.
  A single customer's lane has no meaningful absolute line — it ships a handful of lines a day, so its rate is
  mostly 0 or 1 — but "a quarter below your own normal, every day for a working week" is a line a business can
  state, defend and change. It is a policy rule, not a statistical one: the size and the persistence are
  both chosen in policy.yaml, and crossing it is what makes a lane an incident rather than a curiosity.

A rule may also set `window_days`, in which case the number compared is the segment's volume-weighted mean
over that many trailing days rather than one day's figure. At a fine grain that is not a smoothing trick, it
is the only honest reading: a lane shipping four lines a day has a daily OTIF of 0.00, 0.25, 0.50, 0.75 or
1.00, so a rule about "days in a row below a line" is really a rule about how the week's shipments happened to
fall. A week's rate is what a person judges a lane by, and it is what the rule should test.
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


def evaluate(series: SegmentSeries, day: date, metric: str, cfg: DetectConfig,
             grain: str | None = None) -> ThresholdHit | None:
    rule = cfg.threshold_for(metric, grain or series.grain.name)
    i = series.index.get(day)
    if rule is None or i is None:
        return None
    for severity, basis, limit in _rules(rule):
        if limit is None or not _breached(series, i, limit, basis, rule.direction, rule.window_days,
                                          cfg.baseline_days):
            continue
        days, first = _consecutive(series, i, limit, basis, rule, cfg)
        if days >= rule.consecutive_days:
            return ThresholdHit(severity=severity, level=limit, days_in_breach=days, basis=basis,
                                first_breach=first)
    return None


def _rules(rule: Threshold) -> list[tuple[Severity, str, float | None]]:
    """HIGH first: a value past the high line is past the warn line too, and the stronger verdict wins."""
    return [(Severity.HIGH, "level", rule.high), (Severity.HIGH, "abs", rule.high_abs),
            (Severity.HIGH, "z", rule.high_z), (Severity.HIGH, "drop", rule.high_drop),
            (Severity.WARN, "level", rule.warn), (Severity.WARN, "abs", rule.warn_abs),
            (Severity.WARN, "z", rule.warn_z), (Severity.WARN, "drop", rule.warn_drop)]


def _breached(series: SegmentSeries, i: int, limit: float, basis: str, direction: str,
              window_days: int = 1, baseline_days: int = 28) -> bool:
    value = _rolling(series, i, window_days) if window_days > 1 else float(series.values[i])
    if basis == "drop":
        expected = _norm(series, i, window_days, baseline_days)
        if expected is None:
            return False
        gap = expected - value
        return gap >= limit if direction != "up" else -gap >= limit
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


def _norm(series: SegmentSeries, i: int, window_days: int, baseline_days: int) -> float | None:
    """What this segment normally runs at, computed the same way as the number it is compared against.

    A windowed rule compares a volume-weighted week to a volume-weighted month. The daily baseline in
    `series.expected` is a mean of daily ratios, which on a lane shipping four lines a day swings between 0.70
    and 1.00 from one day to the next — comparing a week's rate against that measures the arithmetic, not the
    lane. Like for like, or the rule means nothing.
    """
    if window_days <= 1:
        if series.expected is None or series.valid is None or not bool(series.valid[i]):
            return None
        return float(series.expected[i])
    if int(series.weighted_counts(baseline_days, inclusive=False)[i]) < window_days:
        return None
    norm = series.weighted_means(baseline_days, inclusive=False)[i]
    return None if np.isnan(norm) else float(norm)


def _rolling(series: SegmentSeries, i: int, days: int) -> float:
    """The segment's volume-weighted mean over the trailing `days`, holidays excluded."""
    value = series.weighted_means(days)[i]
    return float(series.values[i]) if np.isnan(value) else float(value)


def _consecutive(series: SegmentSeries, i: int, limit: float, basis: str, rule: Threshold,
                 cfg: DetectConfig) -> tuple[int, date]:
    """How many days running this has been in breach, counting back from day `i`.

    Two kinds of day are not evidence of a breach and so cannot extend a run: a day with no row (a metric that
    was not published says nothing), and a day inside a holiday window (the plants were shut). Without the
    second, a four-day shutdown hands the rule its consecutive days for free, and the first trading day
    afterwards — while the backlog is still clearing — becomes an incident.
    """
    limit_days = max(rule.consecutive_days * 4, 8)  # enough to answer the question; the rest is replay cost
    days, first, cursor = 0, series.dates[i], i
    while cursor >= 0 and days < limit_days:
        if cursor < i and series.dates[cursor] != series.dates[cursor + 1] - timedelta(days=1):
            break
        if series.holiday[cursor]:
            break
        if not _breached(series, cursor, limit, basis, rule.direction, rule.window_days,
                         cfg.baseline_days):
            break
        days, first = days + 1, series.dates[cursor]
        cursor -= 1
    return days, first
