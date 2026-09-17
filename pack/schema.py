"""The evidence pack: the only thing the model is ever shown.

Everything the brief may say has to exist here first, as a number computed in code with a name of its own. The
design follows from one rule (CLAUDE.md principle 2): every claim in a brief cites a metric, and a claim whose
number does not match the metric it cites is rejected. For a validator to be able to check that, three things
must be true of this structure:

1. **Every quotable number has a stable reference.** `facts` is a flat map from a ref such as
   `I1.value` to the number, its unit, and the exact string a writer should use. Not a nested tree — a
   validator resolving `I1.drivers.0.share` through a tree is a validator with bugs in it.
2. **Every fact carries its display form.** The model is asked to copy `74.1%`, not to format `0.7414`.
   Rounding is where a grounded number quietly becomes a wrong one, so rounding happens once, here, in code.
3. **Nothing else is in scope.** No raw tables, no history the model could average, no room to do arithmetic.
   If a number is not a fact in this pack, there is no way to say it and pass the validator.

The pack is also the permission boundary (B-8): a plant manager's pack is built with their plants only, so
what they may not see is absent rather than hidden.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# "points" is a number already expressed in percentage points (yield variance); "rate_change" is the
# difference between two rates, which has to be multiplied out before it can be spoken of in points. Keeping
# them apart is the difference between "0.5 points" and "50.0 points".
Unit = Literal["rate", "rate_change", "percent", "points", "pounds", "days", "count", "date"]


class Fact(BaseModel):
    """One number a brief may cite, with the exact words to use for it."""

    model_config = ConfigDict(frozen=True)

    ref: str
    value: float
    display: str                 # the rounded string the narration must copy verbatim
    unit: Unit
    label: str                   # what this number is, in words a person would use
    metric: str | None = None
    metric_date: date | None = None
    source: str | None = None    # the metrics table and row this came from, for anyone who wants to check


class DriverFact(BaseModel):
    """A segment's share of a movement, as computed by detect/attribute.py."""

    model_config = ConfigDict(frozen=True)

    label: str
    segment: dict[str, str]
    share_pct: float             # share of the performance change — the publishable number
    share_ref: str
    window_value: float
    baseline_value: float
    value_ref: str
    baseline_ref: str
    effect: Literal["rate", "mix"]


class DetectorNote(BaseModel):
    """Which detector said what. The brief may explain *why* something was flagged, but never re-decide it."""

    model_config = ConfigDict(frozen=True)

    detector: str
    severity: str
    detail: str                  # already rendered in code: "below 0.78 for 3 days running"


class PackItem(BaseModel):
    """One incident, with every number it could need already computed and named."""

    model_config = ConfigDict(frozen=True)

    id: str                      # "I1", "I2" — the handle the narration and its citations use
    metric: str
    metric_label: str            # "OTIF" rather than "otif_rate": the model writes for a person
    grain: str
    segment: dict[str, str]
    segment_label: str
    severity: Literal["WARN", "HIGH"]
    direction: Literal["up", "down"]
    anomaly_type: str            # the key policy.yaml uses for allowed actions (week 9)
    window_start: date
    window_end: date
    value_ref: str
    expected_ref: str | None
    delta_ref: str | None
    volume_ref: str
    detectors: list[DetectorNote]
    drivers: list[DriverFact] = Field(default_factory=list)
    driver_note: str | None = None     # e.g. the movement is a mix shift, not a performance change
    policy_note: str | None = None     # why policy held this below incident level (a shutdown, thin volume)
    series: list[tuple[date, float]] = Field(default_factory=list)   # recent days, for context only
    stale: bool = False


class SourceFreshness(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    latest: date
    days_old: int
    stale: bool
    days_ref: str


class EvidencePack(BaseModel):
    """What one morning's brief is allowed to be about."""

    model_config = ConfigDict(frozen=True)

    run_date: date
    generated_at: datetime
    gold_source: str
    audience: str                       # *who* this pack is for ("vp", "plt01") — the id used everywhere
    role: str = "leadership"            # *what* they are; the permission rules read this, people read the id
    plants: list[str]                   # the plants this audience may see; empty means all
    items: list[PackItem]
    facts: dict[str, Fact]
    freshness: list[SourceFreshness]
    any_stale: bool
    baseline_days: int
    items_considered: int               # how many incidents there were before the policy's cap
    items_cap: int
    is_holiday: bool = False            # the run date sits in a holiday window policy.yaml knows about
    calendar_note: str | None = None    # said first, so a shutdown is never reported as news
    quiet_reason: str | None = None     # why there are no items, when there are none

    def fact(self, ref: str) -> Fact | None:
        return self.facts.get(ref)

    @property
    def is_quiet(self) -> bool:
        return not self.items

    def for_prompt(self) -> dict:
        """Exactly what goes into the prompt: no internals, no ranking arithmetic, no uncitable ids."""
        return {
            "run_date": self.run_date.isoformat(),
            "calendar_note": self.calendar_note,
            "data_freshness": [
                {"source": f.source, "latest": f.latest.isoformat(), "days_old": f.days_old,
                 "stale": f.stale, "days_ref": f.days_ref} for f in self.freshness
            ],
            "any_source_stale": self.any_stale,
            "items": [
                {
                    "id": item.id,
                    "metric": item.metric_label,
                    "segment": item.segment_label,
                    "severity": item.severity,
                    "direction": item.direction,
                    "window": [item.window_start.isoformat(), item.window_end.isoformat()],
                    "stale": item.stale,
                    "why_flagged": [d.detail for d in item.detectors],
                    "drivers": [
                        {"segment": d.label, "share_of_change_pct": d.share_pct, "share_ref": d.share_ref,
                         "its_value_ref": d.value_ref, "its_baseline_ref": d.baseline_ref}
                        for d in item.drivers
                    ],
                    "driver_note": item.driver_note,
                    "policy_note": item.policy_note,
                    "refs": {k: v for k, v in
                             {"value": item.value_ref, "expected": item.expected_ref,
                              "change": item.delta_ref, "volume": item.volume_ref}.items() if v},
                }
                for item in self.items
            ],
            "facts": {ref: {"value": f.display, "means": f.label} for ref, f in self.facts.items()},
        }
