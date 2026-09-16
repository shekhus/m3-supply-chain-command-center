"""The detectors against the seeded world: every anomaly the key says is real, and the one it says isn't.

This is not the replay evaluation (that is B-6, scored over the whole window). It is the narrower question a
detector has to pass first: given the days an anomaly is known to occupy, does the right series say so — at
the grain the anomaly actually lives at, which is the part a company-total dashboard gets wrong.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.config import REPO_ROOT
from detect.config import DetectConfig, load_detect_config
from detect.models import Anomaly, Severity
from detect.run import as_frame, build_series, detect_range, incidents, load_frames, warmup_start, window_for

POLICY = REPO_ROOT / "policy.yaml"
METRICS = REPO_ROOT / "data" / "metrics"
GROUND_TRUTH = REPO_ROOT / "data" / "ground_truth" / "anomalies.json"


def _answer_key() -> dict[str, dict]:
    if not GROUND_TRUTH.exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    return {a["anomaly_id"]: a for a in json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["anomalies"]}


@pytest.fixture(scope="module")
def cfg() -> DetectConfig:
    return load_detect_config(POLICY)


@pytest.fixture(scope="module")
def detected(cfg: DetectConfig) -> list[Anomaly]:
    """Every anomaly over the whole window, computed once: the series build is the slow part."""
    if not (METRICS / "daily_otif_total.parquet").exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    frames = load_frames(METRICS)
    series = build_series(frames, cfg)
    return detect_range(series, cfg, warmup_start(frames, cfg), window_for(frames)[1])


def _matching(found: list[Anomaly], spec: dict) -> list[Anomaly]:
    segment = {k: v for k, v in spec["segment"].items() if v != "ALL"}
    start, end = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])
    return [a for a in found
            if a.metric == spec["metric"] and start <= a.metric_date <= end
            and (all(a.segment.get(k) == v for k, v in segment.items()) if segment else not a.segment)]


@pytest.mark.parametrize("anomaly_id", ["A1", "A2", "A3", "A4"])
def test_every_real_seeded_anomaly_is_raised_as_an_incident(detected: list[Anomaly], anomaly_id: str) -> None:
    spec = _answer_key()[anomaly_id]
    assert spec["expect_detection"] is True
    hits = _matching(detected, spec)
    assert hits, f"{anomaly_id} was not detected at all"
    assert max(h.severity for h in hits) is Severity.HIGH, f"{anomaly_id} never reached HIGH"
    # the worst day of the window points the way the answer key says the anomaly moved (single days inside a
    # three-week window rebound above expectation, and a detector that never said so would be hiding them)
    worst = max(hits, key=lambda h: (h.severity, h.rank))
    assert worst.direction == ("up" if spec["observed"]["delta"] > 0 else "down")


def test_the_backlog_build_is_reported_but_deliberately_not_an_incident(detected: list[Anomaly]) -> None:
    """A6 is found, and stops at WARN — because policy.yaml draws no HIGH line for backlog, on purpose.

    Measured in the replay (docs/decisions.md B-006): the seeded order surge peaks at z=2.81 while ordinary
    backlog swings reach 5.04, so no line separates them. Rather than tune a rule that cannot work, backlog is
    watched and reported, and nobody is woken for it. This test exists so that removing that reasoning from
    the policy is a decision somebody makes, not a change that slips through.
    """
    hits = _matching(detected, _answer_key()["A6"])
    assert hits, "A6 was not detected at all"
    assert max(h.severity for h in hits) is Severity.WARN


@pytest.mark.parametrize("anomaly_id", ["A1", "A2", "A3", "A4", "A6"])
def test_every_real_anomaly_is_caught_within_a_week_of_starting(detected: list[Anomaly],
                                                                anomaly_id: str) -> None:
    """Lead time is what matters operationally: a correct flag three weeks late is a post-mortem."""
    spec = _answer_key()[anomaly_id]
    start = date.fromisoformat(spec["start"])
    flagged = [h.metric_date for h in _matching(detected, spec) if h.severity >= Severity.WARN]
    assert flagged, f"{anomaly_id} produced no WARN or HIGH"
    assert (min(flagged) - start).days <= 7


def test_the_holiday_decoy_is_reported_but_never_as_an_incident(detected: list[Anomaly]) -> None:
    """A5 is a real dip over a shutdown. Flagging it HIGH counts against precision in the answer key."""
    spec = _answer_key()["A5"]
    assert spec["expect_detection"] is False
    start, end = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])

    otif_in_window = [a for a in detected if a.metric == "otif_rate" and start <= a.metric_date <= end]
    assert otif_in_window, "the dip should still be seen and recorded"
    assert max(a.severity for a in otif_in_window) is Severity.WARN
    # the dip is real and is still on the record, with its numbers
    assert any(a.delta is not None and a.delta < 0 for a in otif_in_window)
    # and the findings that would otherwise have been incidents say which rule held them back
    assert any(a.suppressed_reason for a in otif_in_window)


def test_the_decoy_would_have_been_an_incident_without_the_seasonality_policy(cfg: DetectConfig) -> None:
    """The decoy only works as a test if the naive detector fails it — otherwise it proves nothing."""
    spec = _answer_key()["A5"]
    start, end = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])
    from dataclasses import replace

    naive = replace(cfg, holidays=frozenset(), day_of_week_adjust=False)
    frames = load_frames(METRICS)
    found = detect_range(build_series(frames, naive), naive, start, end)
    assert any(a.metric == "otif_rate" and a.severity is Severity.HIGH for a in found)


def test_detection_is_deterministic(cfg: DetectConfig, detected: list[Anomaly]) -> None:
    frames = load_frames(METRICS)
    again = detect_range(build_series(frames, cfg), cfg, warmup_start(frames, cfg), window_for(frames)[1])
    assert [a.to_dict() for a in again] == [a.to_dict() for a in detected]


def test_detectors_stay_silent_until_a_full_baseline_exists(cfg: DetectConfig,
                                                            detected: list[Anomaly]) -> None:
    frames = load_frames(METRICS)
    first_day = window_for(frames)[0]
    assert min(a.metric_date for a in detected) >= warmup_start(frames, cfg)
    early = detect_range(build_series(frames, cfg), cfg, first_day, warmup_start(frames, cfg))
    assert all(a.metric_date >= first_day for a in early)


def test_incidents_are_ordered_for_a_person_and_carry_their_evidence(detected: list[Anomaly]) -> None:
    ranked = incidents(detected)
    assert ranked and all(a.severity >= Severity.WARN for a in ranked)
    assert [a.severity for a in ranked] == sorted((a.severity for a in ranked), reverse=True)
    assert all(a.evidence_refs for a in ranked)

    frame = as_frame(ranked[:50])
    assert set(frame["severity"]) <= {"WARN", "HIGH"}
    assert frame["segment"].notna().all()
