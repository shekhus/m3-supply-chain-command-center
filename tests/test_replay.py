"""The replay's scoring rules, then the replay itself.

Scoring is where an evaluation gets flattered by accident, so each rule is tested against a case built to
break it: an event that merely brushes a seeded window, a company-total wobble claiming credit for a lane
collapse, a flag raised before the anomaly began. Then the whole thing runs over the generated world and the
targets the plan set are asserted — including on the half of the history the policy was never tuned against.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.config import REPO_ROOT
from detect.config import DetectConfig, load_detect_config
from detect.models import Anomaly, Severity
from detect.run import load_frames
from replay.run_replay import TARGETS, ReplayResult, replay, split_scores, summarise, write_report
from replay.scoring import Event, brief_view, matches, score, to_events

POLICY = REPO_ROOT / "policy.yaml"
METRICS = REPO_ROOT / "data" / "metrics"
GROUND_TRUTH = REPO_ROOT / "data" / "ground_truth" / "anomalies.json"
DAY = date(2026, 3, 2)


@pytest.fixture(scope="module")
def cfg() -> DetectConfig:
    return load_detect_config(POLICY)


def _anomaly(day: date, severity: Severity = Severity.HIGH, metric: str = "otif_rate",
             grain: str = "otif_lane", segment: dict | None = None, rank: float = 1.0) -> Anomaly:
    """`rank` is what the anomaly should rank at: magnitude is set to produce it, since rank is computed."""
    materiality = 0.05
    anomaly = Anomaly(metric=metric, grain=grain,
                      segment={"plant": "PLT-02", "customer_no": "C000031"} if segment is None else segment,
                      metric_date=day, window_start=day, severity=severity, value=0.5, expected=0.9,
                      delta=-0.4, direction="down", volume=50.0, magnitude=rank * materiality,
                      materiality=materiality, volume_scale=50.0)
    assert anomaly.rank == pytest.approx(rank)
    return anomaly


def _spec(**over) -> dict:
    spec = {"anomaly_id": "A1", "metric": "otif_rate",
            "segment": {"plant": "PLT-02", "customer_no": "C000031"},
            "start": "2026-03-01", "end": "2026-03-21", "expect_detection": True}
    spec.update(over)
    return spec


# --- events -----------------------------------------------------------------------------------


def test_a_run_of_flags_is_one_event_not_twenty_one() -> None:
    days = [_anomaly(DAY + timedelta(days=i)) for i in range(21)]
    events = to_events(days)
    assert len(events) == 1
    assert events[0].days == 21 and events[0].start == DAY and events[0].end == DAY + timedelta(days=20)
    assert events[0].peak_severity is Severity.HIGH


def test_a_short_gap_is_bridged_and_a_long_one_starts_a_new_event() -> None:
    """A metric not published on a Sunday has not recovered; three weeks later is a different incident."""
    short = to_events([_anomaly(DAY), _anomaly(DAY + timedelta(days=2))])
    assert len(short) == 1

    apart = to_events([_anomaly(DAY), _anomaly(DAY + timedelta(days=20))])
    assert len(apart) == 2


def test_findings_below_warn_never_become_events() -> None:
    assert to_events([_anomaly(DAY, Severity.INFO)]) == []


def test_two_segments_are_never_merged_into_one_event() -> None:
    other = {"plant": "PLT-01", "customer_no": "C000002"}
    events = to_events([_anomaly(DAY), _anomaly(DAY, segment=other)])
    assert len(events) == 2


# --- matching ---------------------------------------------------------------------------------


def _event(anomalies: list[Anomaly]) -> Event:
    return to_events(anomalies)[0]


def test_an_event_at_a_coarser_grain_still_matches_the_anomaly_underneath_it() -> None:
    plant = _event([_anomaly(DAY, grain="otif_plant", segment={"plant": "PLT-02"})])
    assert matches(plant, _spec())


def test_a_company_total_wobble_does_not_get_credit_for_a_lane_collapse() -> None:
    """Sharing no key with the seeded segment means it is a different question, however well the dates line up."""
    total = _event([_anomaly(DAY, grain="otif_total", segment={})])
    assert not matches(total, _spec())


def test_the_wrong_segment_never_matches() -> None:
    elsewhere = _event([_anomaly(DAY, segment={"plant": "PLT-01", "customer_no": "C000031"})])
    assert not matches(elsewhere, _spec())


def test_the_wrong_metric_never_matches() -> None:
    other = _event([_anomaly(DAY, metric="fill_rate_weight")])
    assert not matches(other, _spec())


def test_an_event_outside_the_window_never_matches() -> None:
    late = _event([_anomaly(date(2026, 5, 1))])
    assert not matches(late, _spec())


def test_a_company_wide_anomaly_is_matched_by_any_segment_of_its_metric() -> None:
    """A5 is company-wide: a lane flagged during the shutdown is the same dip, seen closer up."""
    lane = _event([_anomaly(DAY)])
    assert matches(lane, _spec(segment={"plant": "ALL"}, expect_detection=False))


# --- scoring ----------------------------------------------------------------------------------


def test_recall_lead_time_and_precision_on_a_hand_built_case() -> None:
    hit = [_anomaly(date(2026, 3, 4) + timedelta(days=i)) for i in range(3)]
    unrelated = [_anomaly(date(2026, 6, 1) + timedelta(days=i), metric="fill_rate_weight",
                          grain="fill_weight_group",
                          segment={"plant": "PLT-01", "product_group": "GROUND"}) for i in range(2)]
    card = score(to_events(hit + unrelated), [_spec()], (date(2026, 3, 1), date(2026, 6, 30)))

    assert card.recall == 1.0
    assert card.findings[0].lead_time_days == 3          # seeded 2026-03-01, first flagged 2026-03-04
    assert card.true_positive_events == 1 and card.false_positive_events == 1
    assert card.precision == 0.5
    assert card.median_lead_time == 3


def test_a_flag_raised_before_the_anomaly_began_is_not_counted_as_prescience() -> None:
    """An event already running when the anomaly started belongs to whatever came before it."""
    early = [_anomaly(date(2026, 2, 25) + timedelta(days=i)) for i in range(10)]
    card = score(to_events(early), [_spec()], (date(2026, 2, 1), date(2026, 3, 30)))
    assert card.findings[0].detected
    assert card.findings[0].lead_time_days == 0          # measured from the seeded start, never negative


def test_an_undetected_anomaly_is_reported_as_missed() -> None:
    card = score([], [_spec()], (date(2026, 3, 1), date(2026, 3, 30)))
    assert card.recall == 0.0
    assert card.findings[0].detected is False and card.findings[0].lead_time_days is None
    assert card.median_lead_time is None


def test_a_warn_only_event_counts_for_recall_but_not_for_precision() -> None:
    warns = [_anomaly(date(2026, 3, 4) + timedelta(days=i), Severity.WARN) for i in range(3)]
    card = score(to_events(warns), [_spec()], (date(2026, 3, 1), date(2026, 3, 30)))
    assert card.recall == 1.0
    assert card.high_events == 0 and card.true_positive_events == 0


def test_the_decoy_counts_against_precision_only_on_its_own_days() -> None:
    decoy = _spec(anomaly_id="A5", segment={"plant": "ALL"}, start="2026-07-02", end="2026-07-06",
                  expect_detection=False)
    inside = to_events([_anomaly(date(2026, 7, 3) + timedelta(days=i)) for i in range(2)])
    card = score(inside, [decoy], (date(2026, 6, 1), date(2026, 7, 30)))
    assert card.decoy_high_events == 1
    assert card.false_positive_events == 1               # never a true positive: the key says not to raise it

    before = to_events([_anomaly(date(2026, 6, 24) + timedelta(days=i)) for i in range(3)])
    card_before = score(before, [decoy], (date(2026, 6, 1), date(2026, 7, 30)))
    assert card_before.decoy_high_events == 0            # raised for its own reasons, days before the shutdown


def test_the_brief_view_counts_only_what_a_person_would_have_been_shown() -> None:
    """Six items a day: a real incident ranked seventh was never seen, and is not credit the system earns."""
    real = [_anomaly(date(2026, 3, 4), rank=0.1)]
    noise = [_anomaly(date(2026, 3, 4), rank=float(10 + i), grain="fill_weight_group",
                      metric="fill_rate_weight", segment={"plant": "PLT-01", "product_group": f"G{i}"})
             for i in range(8)]
    shown, correct = brief_view(to_events(real + noise), [_spec()], per_day=6)
    assert shown == 6 and correct == 0

    shown_all, correct_all = brief_view(to_events(real), [_spec()], per_day=6)
    assert shown_all == 1 and correct_all == 1


# --- the replay over the generated world ------------------------------------------------------


def _specs() -> list[dict]:
    if not GROUND_TRUTH.exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["anomalies"]


@pytest.fixture(scope="module")
def full(cfg: DetectConfig) -> ReplayResult:
    if not (METRICS / "daily_otif_total.parquet").exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    return replay(load_frames(METRICS), cfg, _specs())


def test_the_replay_meets_every_target_the_plan_set(full: ReplayResult) -> None:
    card = full.card
    assert card.recall >= TARGETS["recall"], f"recall {card.recall:.0%}"
    assert card.precision >= TARGETS["precision"], f"precision {card.precision:.0%}"
    assert card.attribution_accuracy >= TARGETS["attribution_accuracy"]
    assert card.decoy_high_events == 0, "the holiday decoy was raised as an incident"
    assert all(full.met_targets.values())


def test_every_seeded_anomaly_is_found_and_the_decoy_is_not_raised(full: ReplayResult) -> None:
    by_id = {f.anomaly_id: f for f in full.card.findings}
    for anomaly_id in ("A1", "A2", "A3", "A4", "A6"):
        assert by_id[anomaly_id].detected, f"{anomaly_id} was missed"
    assert by_id["A5"].detected           # the dip is seen ...
    assert by_id["A5"].peak_severity == "WARN"   # ... and never raised as an incident


def test_the_median_lead_time_is_within_a_working_week(full: ReplayResult) -> None:
    assert full.card.median_lead_time is not None
    assert full.card.median_lead_time <= 7


def test_the_held_out_half_was_never_tuned_against_and_still_holds(cfg: DetectConfig) -> None:
    """The policy's floors were chosen by looking at the first window; this asserts the second one."""
    if not (METRICS / "daily_otif_total.parquet").exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    _tuned, held = split_scores(load_frames(METRICS), cfg, _specs(), date(2026, 2, 28))
    seeded_here = [f for f in held.card.findings if f.expect_detection
                   and f.anomaly_id in {"A4", "A6"}]
    assert all(f.detected for f in seeded_here)
    assert held.card.precision >= TARGETS["precision"]
    assert held.card.decoy_high_events == 0


def test_the_report_states_the_numbers_and_the_failures(full: ReplayResult, cfg: DetectConfig,
                                                        tmp_path) -> None:
    path = tmp_path / "REPLAY.md"
    write_report(full, cfg, path, tmp_path / "replay.json", split_scores(
        load_frames(METRICS), cfg, _specs(), date(2026, 2, 28)), _specs())
    text = path.read_text(encoding="utf-8")

    assert "Detection recall" in text and "Holiday decoy raised HIGH" in text
    assert "Tuned on the first half" in text
    assert "What the numbers do not say" in text
    for anomaly_id in ("A1", "A2", "A3", "A4", "A5", "A6"):
        assert anomaly_id in text
    assert f"baseline {cfg.baseline_days} days" in text       # the settings that produced these numbers
    assert json.loads((tmp_path / "replay.json").read_text(encoding="utf-8"))["score"]["recall"] == 1.0


def test_the_summary_line_names_what_failed(full: ReplayResult) -> None:
    assert "all targets met" in summarise(full)
