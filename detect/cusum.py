"""Detector 3: CUSUM change-point on the standardised series.

The baseline detector sees a day. CUSUM sees a *shift*: it accumulates small standardised deviations and fires
when the total is too large to be noise, which catches a drop that is only half a standard deviation a day and
has not gone away in a week — the shape of A2's weight shortfall and A4's stalled lots. In exchange it is
slower on a one-day cliff, which is why all three detectors run and severity takes the strongest.

Standard tabular CUSUM: S⁺ₜ = max(0, S⁺ₜ₋₁ + zₜ − k), S⁻ₜ = max(0, S⁻ₜ₋₁ − zₜ − k), fire at h. `k` is the
slack in standard deviations (drifts smaller than k are ignored), `h` the decision interval; both from policy.

The change point is the day the accumulation last started from zero — the answer to "when did this begin?",
which is what lead time is measured from and what the brief says out loud. Holidays are skipped: a shutdown is
not a shift, and letting one push the accumulator would make every January look like a change point.

**The accumulator resets when it signals**, as the procedure requires. Without the reset a single shift keeps
the statistic above the decision interval for as long as the shift lasts, and the detector reports the same
change every day for a fortnight — which is how a change-point detector turns into a stuck alarm.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from detect.config import DetectConfig
from detect.models import Severity
from detect.series import SegmentSeries


@dataclass(frozen=True)
class CusumHit:
    severity: Severity
    statistic: float
    direction: str          # "down" or "up" — the arm that fired
    change_point: date
    run_days: int


def evaluate(series: SegmentSeries, day: date, cfg: DetectConfig, lookback_days: int = 60) -> CusumHit | None:
    i = series.index.get(day)
    if i is None or series.z is None or series.valid is None:
        return None
    start = series.window(i, lookback_days).start

    s_up = s_down = 0.0
    start_up = start_down = series.dates[i]
    for j in range(start, i + 1):
        if series.holiday[j] or not bool(series.valid[j]) or np.isnan(series.z[j]):
            continue
        if s_up <= 0:
            start_up = series.dates[j]
        if s_down <= 0:
            start_down = series.dates[j]
        z = float(series.z[j])
        s_up = max(0.0, s_up + z - cfg.cusum_k)
        s_down = max(0.0, s_down - z - cfg.cusum_k)

    statistic, direction, began = max((s_down, "down", start_down), (s_up, "up", start_up),
                                      key=lambda arm: arm[0])
    if statistic < cfg.cusum_h:
        return None
    severity = Severity.HIGH if statistic >= 2 * cfg.cusum_h else Severity.WARN
    return CusumHit(severity=severity, statistic=statistic, direction=direction, change_point=began,
                    run_days=(day - began).days + 1)
