"""Scoring a replay against the answer key — and the definitions that make the numbers mean something.

Every measurement here can be made to look better by choosing a kinder definition, so the definitions are
written down first and the code follows them:

**An event, not a flag.** A three-week lane collapse produces a flag on each of twenty-one days. Counting
those as twenty-one findings would make recall look effortless and precision look catastrophic, and neither
number would describe what a person experiences. Consecutive flags for the same metric and segment are merged
into one event (gaps of up to `MERGE_GAP_DAYS` bridged, because a metric that is not published on a Sunday has
not recovered).

**A match needs the segment to agree.** An event matches a seeded anomaly when the metric is the same, the
windows overlap, and the segments agree on every key they share — an event at a coarser grain (PLT-02) matches
a lane-level anomaly inside that plant, because that is genuinely the same incident seen from further away. It
must share at least one key: otherwise a company-total wobble would "detect" every anomaly in the world, which
is the kind of scoring that makes a system look good and a morning useless. A seeded anomaly with no segment
(A5 is company-wide) is matched by any event on its metric.

**Recall is measured at WARN or above; precision at HIGH.** That asymmetry is the answer key's, and it is
the right one: recall asks whether the system noticed, precision asks whether it interrupted someone. An event
that never rose above WARN was recorded and never put in front of a person.

**A5 counts against precision if it reaches HIGH.** The holiday dip is real, so it is not a hallucination —
but raising it as an incident is exactly the failure the decoy exists to catch, and it is scored as one. That
test is made on the *flags inside the decoy's own days*: an event that began a week earlier and runs into the
holiday was raised for its own reasons, and blaming the holiday for it would flatter the detector.

**Lead time is measured from the anomaly's start**, not from the day the detector happened to look. Median,
not mean, because one slow detection should not be averaged away by four fast ones.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from detect.models import Anomaly, Severity

MERGE_GAP_DAYS = 3


@dataclass
class Event:
    """One continuous run of flags for a single metric and segment: what a person would call an incident."""

    metric: str
    grain: str
    segment: dict[str, str]
    start: date
    end: date
    peak_severity: Severity
    peak_rank: float
    first_warn: date
    first_high: date | None
    days: int
    anomalies: list[Anomaly] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "/".join(f"{k}={v}" for k, v in sorted(self.segment.items())) or "ALL"

    @property
    def peak(self) -> Anomaly:
        return max(self.anomalies, key=lambda a: (a.severity, a.rank))

    def to_dict(self) -> dict:
        return {"metric": self.metric, "grain": self.grain, "segment": self.segment, "label": self.label,
                "start": self.start.isoformat(), "end": self.end.isoformat(),
                "peak_severity": self.peak_severity.label, "peak_rank": self.peak_rank, "days": self.days,
                "first_warn": self.first_warn.isoformat(),
                "first_high": self.first_high.isoformat() if self.first_high else None}


def to_events(anomalies: list[Anomaly], gap_days: int = MERGE_GAP_DAYS) -> list[Event]:
    """Merge day-by-day flags at WARN or above into events, one per metric/segment run."""
    by_key: dict[tuple, list[Anomaly]] = {}
    for anomaly in anomalies:
        if anomaly.severity < Severity.WARN:
            continue
        by_key.setdefault(anomaly.key, []).append(anomaly)

    events: list[Event] = []
    for run in by_key.values():
        run.sort(key=lambda a: a.metric_date)
        current: list[Anomaly] = []
        for anomaly in run:
            if current and (anomaly.metric_date - current[-1].metric_date).days > gap_days:
                events.append(_event(current))
                current = []
            current.append(anomaly)
        if current:
            events.append(_event(current))
    return sorted(events, key=lambda e: (e.start, -e.peak_severity, -e.peak_rank))


def _event(run: list[Anomaly]) -> Event:
    first = run[0]
    highs = [a.metric_date for a in run if a.severity is Severity.HIGH]
    return Event(metric=first.metric, grain=first.grain, segment=first.segment,
                 start=run[0].metric_date, end=run[-1].metric_date,
                 peak_severity=max(a.severity for a in run), peak_rank=max(a.rank for a in run),
                 first_warn=run[0].metric_date, first_high=min(highs) if highs else None,
                 days=len(run), anomalies=list(run))


def matches(event: Event, spec: dict) -> bool:
    """Is this event the seeded anomaly, seen at whatever grain the detector happened to see it?"""
    if event.metric != spec["metric"]:
        return False
    seeded = {k: v for k, v in spec["segment"].items() if v != "ALL"}
    start, end = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])
    if event.end < start or event.start > end:
        return False
    if not seeded:
        return True                                   # a company-wide anomaly is visible in every segment
    shared = set(seeded) & set(event.segment)
    if not shared:
        return False                                  # no key in common: a different question entirely
    return all(event.segment[k] == seeded[k] for k in shared)


@dataclass
class Finding:
    """What the replay concluded about one seeded anomaly."""

    anomaly_id: str
    metric: str
    expect_detection: bool
    detected: bool
    peak_severity: str | None
    lead_time_days: int | None
    first_flag: date | None
    events: int
    top_driver: str | None = None
    driver_correct: bool | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        return {**self.__dict__, "first_flag": self.first_flag.isoformat() if self.first_flag else None}


@dataclass
class ScoreCard:
    window: tuple[date, date]
    findings: list[Finding]
    events: int
    high_events: int
    true_positive_events: int
    false_positive_events: int
    decoy_high_events: int
    brief_items: int = 0
    brief_true_positives: int = 0

    @property
    def recall(self) -> float:
        expected = [f for f in self.findings if f.expect_detection]
        return sum(f.detected for f in expected) / len(expected) if expected else 0.0

    @property
    def precision(self) -> float:
        """Of the events that would have interrupted somebody, how many were real."""
        raised = self.true_positive_events + self.false_positive_events
        return self.true_positive_events / raised if raised else 0.0

    @property
    def brief_precision(self) -> float:
        """Of the items a person would actually have been shown, how many were real. The lived number."""
        return self.brief_true_positives / self.brief_items if self.brief_items else 0.0

    @property
    def median_lead_time(self) -> float | None:
        leads = [f.lead_time_days for f in self.findings
                 if f.expect_detection and f.detected and f.lead_time_days is not None]
        return statistics.median(leads) if leads else None

    @property
    def attribution_accuracy(self) -> float:
        scored = [f for f in self.findings if f.driver_correct is not None]
        return sum(bool(f.driver_correct) for f in scored) / len(scored) if scored else 0.0

    @property
    def false_positives_per_day(self) -> float:
        days = max((self.window[1] - self.window[0]).days, 1)
        return self.false_positive_events / days

    def to_dict(self) -> dict:
        return {
            "window": [self.window[0].isoformat(), self.window[1].isoformat()],
            "recall": self.recall, "precision": self.precision, "brief_precision": self.brief_precision,
            "median_lead_time_days": self.median_lead_time,
            "attribution_accuracy": self.attribution_accuracy,
            "events": self.events, "high_events": self.high_events,
            "true_positive_events": self.true_positive_events,
            "false_positive_events": self.false_positive_events,
            "false_positives_per_day": self.false_positives_per_day,
            "decoy_high_events": self.decoy_high_events,
            "brief_items": self.brief_items, "brief_true_positives": self.brief_true_positives,
            "findings": [f.to_dict() for f in self.findings],
        }


def score(events: list[Event], specs: list[dict], window: tuple[date, date]) -> ScoreCard:
    """Events in, the answer key's questions answered. Attribution is filled in by the caller."""
    findings = []
    for spec in specs:
        hits = [e for e in events if matches(e, spec)]
        warned = [e for e in hits if e.peak_severity >= Severity.WARN]
        start = date.fromisoformat(spec["start"])
        # the first flag raised *on or after* the day the anomaly began. An event already running when it
        # started belongs to whatever was happening before, and crediting it would report a negative lead time
        # and call it prescience.
        flags = [a.metric_date for e in warned for a in e.anomalies
                 if a.severity >= Severity.WARN and a.metric_date >= start]
        first = min(flags, default=None)
        findings.append(Finding(
            anomaly_id=spec["anomaly_id"], metric=spec["metric"],
            expect_detection=bool(spec["expect_detection"]), detected=bool(warned),
            peak_severity=max((e.peak_severity for e in hits), default=Severity.INFO).label if hits else None,
            lead_time_days=(first - start).days if first else None, first_flag=first, events=len(hits)))

    real = [s for s in specs if s["expect_detection"]]
    high = [e for e in events if e.peak_severity is Severity.HIGH]
    true_positives = [e for e in high if any(matches(e, s) for s in real)]
    decoys = [e for e in high if any(_high_inside(e, s) for s in specs if not s["expect_detection"])]
    return ScoreCard(window=window, findings=findings, events=len(events), high_events=len(high),
                     true_positive_events=len(true_positives),
                     false_positive_events=len(high) - len(true_positives),
                     decoy_high_events=len(decoys))


def brief_view(events: list[Event], specs: list[dict], per_day: int) -> tuple[int, int]:
    """What a person would actually have been shown: the top `per_day` incidents each day, by rank.

    Scored separately because it is the number they live with — an event that was real but ranked seventh was
    never seen, and an event that was wrong but ranked first cost somebody their morning.
    """
    real = [s for s in specs if s["expect_detection"]]
    by_day: dict[date, list[tuple[float, Event]]] = {}
    for event in events:
        for anomaly in event.anomalies:
            if anomaly.severity is Severity.HIGH:
                by_day.setdefault(anomaly.metric_date, []).append((anomaly.rank, event))

    shown = correct = 0
    for day, items in by_day.items():
        items.sort(key=lambda pair: -pair[0])
        seen: set[tuple] = set()
        for _rank, event in items:
            if event.peak.key in seen:
                continue
            seen.add(event.peak.key)
            if len(seen) > per_day:
                break
            shown += 1
            correct += any(matches(event, s) and _covers(s, day) for s in real)
    return shown, correct


def _high_inside(event: Event, spec: dict) -> bool:
    """A HIGH flag on one of the decoy's own days — the failure the answer key actually names."""
    if event.metric != spec["metric"]:
        return False
    start, end = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])
    return any(a.severity is Severity.HIGH and start <= a.metric_date <= end for a in event.anomalies)


def _covers(spec: dict, day: date) -> bool:
    return date.fromisoformat(spec["start"]) <= day <= date.fromisoformat(spec["end"]) + timedelta(days=2)
