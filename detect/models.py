"""What a detector produces. No LLM ever writes one of these, and no LLM ever changes one.

A `Signal` is one detector's opinion about one segment on one day. An `Anomaly` is what the three detectors
between them say about that segment-day: the severity is the strongest opinion, and the evidence refs point
back at the exact metric rows a claim may cite later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import IntEnum


class Severity(IntEnum):
    """Ordered so `max()` means what it says. INFO is recorded, never surfaced as an incident."""

    INFO = 0
    WARN = 1
    HIGH = 2

    @property
    def label(self) -> str:
        return self.name


@dataclass(frozen=True)
class EvidenceRef:
    """Where a number came from: table, segment, day. Claims resolve against these."""

    table: str
    segment: dict[str, str]
    metric_date: date
    column: str

    @property
    def ref(self) -> str:
        keys = "/".join(f"{k}={v}" for k, v in sorted(self.segment.items())) or "ALL"
        return f"{self.table}:{keys}:{self.metric_date.isoformat()}:{self.column}"


@dataclass
class Signal:
    detector: str
    severity: Severity
    value: float
    expected: float | None = None
    delta: float | None = None
    z: float | None = None
    detail: dict[str, float | str | bool] = field(default_factory=dict)


@dataclass
class Anomaly:
    metric: str
    grain: str
    segment: dict[str, str]
    metric_date: date          # the day the detectors ran on
    window_start: date         # earliest day of the run this flag belongs to (the change point, when known)
    severity: Severity
    value: float
    expected: float | None
    delta: float | None
    direction: str             # "down" or "up" — which way the metric moved
    volume: float              # lines, pounds or lots behind the number: the "does it matter" factor
    magnitude: float           # |delta|, in the metric's own units
    volume_scale: float = 1.0  # a typical segment's volume at this grain, so segments compare across metrics
    materiality: float = 0.0   # policy's min_magnitude: the unit `relative_magnitude` counts in
    detectors: list[Signal] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    suppressed_reason: str | None = None   # why a severity was capped (holiday allowance, thin volume)

    @property
    def key(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        return (self.metric, self.grain, tuple(sorted(self.segment.items())))

    @property
    def relative_magnitude(self) -> float:
        """The move counted in multiples of what policy calls a material move for this metric.

        Not a fraction of the expected value: `lb_at_risk_within_5d` and `yield_variance_pct` both sit near
        zero for weeks at a time, and dividing by an expectation of almost nothing turns an ordinary movement
        into a ratio of thousands. The materiality floor is a number the business set, is never near zero, and
        makes "three times material" mean the same thing for pounds and for percentage points.
        """
        scale = self.materiality or abs(self.expected or 0.0)
        return self.magnitude / scale if scale > 0 else self.magnitude

    @property
    def rank(self) -> float:
        """What makes one anomaly worth a leader's morning: how many material moves it made, times how
        big this segment is next to a typical one at the same grain.

        Both factors are dimensionless on purpose. Ranking on magnitude × volume in each metric's own units
        means pounds always beat percentage points, and the brief leads every morning with whichever metric
        happens to have the largest unit — a fact about arithmetic, not about the business.
        """
        weight = self.volume / self.volume_scale if self.volume_scale > 0 else 1.0
        return self.relative_magnitude * max(weight, 0.05)

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "grain": self.grain,
            "segment": self.segment,
            "metric_date": self.metric_date.isoformat(),
            "window_start": self.window_start.isoformat(),
            "severity": self.severity.label,
            "value": self.value,
            "expected": self.expected,
            "delta": self.delta,
            "direction": self.direction,
            "volume": self.volume,
            "magnitude": self.magnitude,
            "relative_magnitude": self.relative_magnitude,
            "volume_scale": self.volume_scale,
            "materiality": self.materiality,
            "rank": self.rank,
            "detectors": [
                {"detector": s.detector, "severity": s.severity.label, "value": s.value,
                 "expected": s.expected, "delta": s.delta, "z": s.z,
                 **{f"detail_{k}": v for k, v in s.detail.items()}}
                for s in self.detectors
            ],
            "evidence_refs": self.evidence_refs,
            "suppressed_reason": self.suppressed_reason,
        }
