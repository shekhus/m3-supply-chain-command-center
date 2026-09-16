"""Attribution: the decomposition on paper, the identity that makes it publishable, and the seeded causes.

The number in a brief ("this lane is 71% of the drop") is only worth printing if it comes from an identity
that closes. These tests check that it does — on a four-segment example small enough to verify by hand, and
on the generated world, where the answer key names the segment the arithmetic has to find.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from app.config import REPO_ROOT
from detect.attribute import CHILDREN, Attribution, attribute, attribute_all, child_grain
from detect.config import DetectConfig, load_detect_config
from detect.models import Anomaly, Severity
from detect.run import build_series, detect_range, load_frames
from detect.series import GRAINS, Grain

POLICY = REPO_ROOT / "policy.yaml"
METRICS = REPO_ROOT / "data" / "metrics"
GROUND_TRUTH = REPO_ROOT / "data" / "ground_truth" / "anomalies.json"
LANE = next(g for g in GRAINS if g.name == "otif_lane")
PLANT = next(g for g in GRAINS if g.name == "otif_plant")


@pytest.fixture(scope="module")
def cfg() -> DetectConfig:
    return load_detect_config(POLICY)


# --- the arithmetic ---------------------------------------------------------------------------


def _frames(rows: list[dict]) -> dict[str, pd.DataFrame]:
    """Both grains from one set of lane rows, so the parent is genuinely the aggregate of its children."""
    lanes = pd.DataFrame(rows)
    lanes["otif_rate"] = lanes["otif_lines"] / lanes["lines"]
    plant = (lanes.groupby(["metric_date", "plant"], as_index=False)
             .agg(otif_lines=("otif_lines", "sum"), lines=("lines", "sum")))
    plant["otif_rate"] = plant["otif_lines"] / plant["lines"]
    return {"daily_otif_lane": lanes, "daily_otif_plant": plant}


def _flat(baseline_rows: list[tuple[str, int, int]], window_rows: list[tuple[str, int, int]],
          cfg: DetectConfig) -> tuple[dict[str, pd.DataFrame], Anomaly]:
    """One baseline day and one window day, both outside any holiday, at a single plant."""
    base_day, window_day = date(2026, 3, 3), date(2026, 3, 31)
    assert not cfg.is_holiday(base_day) and not cfg.is_holiday(window_day)
    rows = [{"metric_date": base_day, "plant": "PLT-01", "customer_no": c, "lines": n, "otif_lines": k}
            for c, n, k in baseline_rows]
    rows += [{"metric_date": window_day, "plant": "PLT-01", "customer_no": c, "lines": n, "otif_lines": k}
             for c, n, k in window_rows]
    anomaly = Anomaly(metric="otif_rate", grain="otif_plant", segment={"plant": "PLT-01"},
                      metric_date=window_day, window_start=window_day, severity=Severity.HIGH,
                      value=0.0, expected=0.0, delta=0.0, direction="down", volume=100.0, magnitude=0.0)
    return _frames(rows), anomaly


def test_a_hand_computed_decomposition(cfg: DetectConfig) -> None:
    """Two lanes, 50 lines each. A: 0.90 → 0.70. B: unchanged at 1.00. No volume moves, so no mix effect.

    Plant rate: (45+50)/100 = 0.95 → (35+50)/100 = 0.85, a drop of 0.10, all of it lane A's.
    """
    frames, anomaly = _flat([("A", 50, 45), ("B", 50, 50)], [("A", 50, 35), ("B", 50, 50)], cfg)
    result = attribute(frames, cfg, anomaly)

    assert result.grain == "otif_lane"
    assert result.total_delta == pytest.approx(-0.10)
    top = result.drivers[0]
    assert top.segment == {"plant": "PLT-01", "customer_no": "A"}
    assert top.rate_effect == pytest.approx(-0.10)       # 0.5 × (0.70 − 0.90)
    assert top.mix_effect == pytest.approx(0.0)
    assert top.contribution_pct == pytest.approx(100.0)
    assert top.rate_effect_pct == pytest.approx(100.0)
    assert top.baseline_value == pytest.approx(0.90) and top.window_value == pytest.approx(0.70)
    assert result.dominant_effect == "rate"
    assert result.explained_pct == pytest.approx(100.0)


def test_a_pure_mix_shift_is_named_as_one(cfg: DetectConfig) -> None:
    """Nobody performed differently; the bad lane simply shipped more. Blaming it would be wrong.

    A holds 0.80 and B holds 1.00 throughout. Volume moves from 20/80 to 80/20, so the plant rate falls from
    0.96 to 0.84 without a single lane changing.
    """
    frames, anomaly = _flat([("A", 20, 16), ("B", 80, 80)], [("A", 80, 64), ("B", 20, 20)], cfg)
    result = attribute(frames, cfg, anomaly)

    assert result.total_delta == pytest.approx(-0.12)
    assert result.rate_effect_total == pytest.approx(0.0, abs=1e-12)
    assert result.mix_effect_total == pytest.approx(-0.12)
    assert result.dominant_effect == "mix"
    assert result.note is not None and "what was shipped" in result.note


def test_the_contributions_account_for_the_whole_movement(cfg: DetectConfig) -> None:
    """The identity closing is what licenses the percentage. Four lanes, rates and volumes both moving."""
    frames, anomaly = _flat([("A", 40, 38), ("B", 30, 29), ("C", 20, 18), ("D", 10, 10)],
                            [("A", 10, 7), ("B", 45, 43), ("C", 25, 20), ("D", 20, 19)], cfg)
    result = attribute(frames, cfg, anomaly, top=4)

    assert len(result.drivers) == 4
    assert sum(d.contribution for d in result.drivers) == pytest.approx(result.total_delta)
    assert sum(d.rate_effect + d.mix_effect for d in result.drivers) == pytest.approx(result.total_delta)
    assert sum(d.rate_effect_pct for d in result.drivers) == pytest.approx(100.0)
    assert result.explained_pct == pytest.approx(100.0)


def test_drivers_are_ranked_by_who_got_worse_not_by_who_moved_the_average(cfg: DetectConfig) -> None:
    """A big mix term on a steady lane must not outrank the lane that actually deteriorated."""
    frames, anomaly = _flat([("STEADY", 10, 10), ("BROKEN", 50, 48)],
                            [("STEADY", 60, 60), ("BROKEN", 50, 30)], cfg)
    result = attribute(frames, cfg, anomaly)
    assert result.drivers[0].segment["customer_no"] == "BROKEN"
    assert abs(result.drivers[1].mix_effect) > abs(result.drivers[0].mix_effect)   # STEADY moved the average


def test_a_segment_that_only_appears_in_one_window_contributes_only_its_mix(cfg: DetectConfig) -> None:
    """A new customer has no baseline rate of its own; it is credited the plant's, so it is not blamed."""
    frames, anomaly = _flat([("A", 100, 95)], [("A", 100, 95), ("NEW", 100, 95)], cfg)
    result = attribute(frames, cfg, anomaly)
    new = next(d for d in result.drivers if d.segment["customer_no"] == "NEW")
    assert new.rate_effect == pytest.approx(0.0)
    assert result.total_delta == pytest.approx(0.0, abs=1e-12)


def test_an_anomaly_at_the_finest_grain_says_so_instead_of_inventing_a_driver(cfg: DetectConfig) -> None:
    frames, anomaly = _flat([("A", 50, 45)], [("A", 50, 35)], cfg)
    lane_anomaly = Anomaly(metric="otif_rate", grain="otif_lane",
                           segment={"plant": "PLT-01", "customer_no": "A"}, metric_date=date(2026, 3, 31),
                           window_start=date(2026, 3, 31), severity=Severity.HIGH, value=0.70, expected=0.90,
                           delta=-0.20, direction="down", volume=50.0, magnitude=0.20)
    result = attribute(frames, cfg, lane_anomaly)
    assert result.drivers == []
    assert result.grain == ""
    assert result.note is not None and "finest grain" in result.note
    assert result.total_delta == pytest.approx(-0.20)


def test_every_declared_child_grain_is_real_and_finer_than_its_parent() -> None:
    for name in CHILDREN:
        parent = next(g for g in GRAINS if g.name == name)
        child = child_grain(parent)
        assert child is not None
        assert set(parent.keys) < set(child.keys)
        assert child.value == parent.value        # the same metric, one level down


def test_a_grain_with_no_child_returns_none() -> None:
    lonely = Grain("solo", "daily_backlog_plant", ("plant",), "open_backlog_lines",
                   "open_backlog_lines", "up")
    assert child_grain(lonely) is None


# --- against the seeded causes ----------------------------------------------------------------


def _answer_key() -> dict[str, dict]:
    if not GROUND_TRUTH.exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    return {a["anomaly_id"]: a for a in json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["anomalies"]}


@pytest.fixture(scope="module")
def world(cfg: DetectConfig) -> tuple[dict[str, pd.DataFrame], list]:
    if not (METRICS / "daily_otif_total.parquet").exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    frames = load_frames(METRICS)
    return frames, build_series(frames, cfg)


def _worst(series: list, cfg: DetectConfig, grain: str, metric: str, segment: dict[str, str],
           span: tuple[date, date]) -> Anomaly:
    found = [a for a in detect_range(series, cfg, *span)
             if a.grain == grain and a.metric == metric and a.severity >= Severity.WARN
             and all(a.segment.get(k) == v for k, v in segment.items())]
    assert found, f"no incident at {grain} for {metric} {segment}"
    return max(found, key=lambda a: (a.severity, a.rank))


@pytest.mark.parametrize(("anomaly_id", "grain", "segment", "expect"), [
    ("A1", "otif_plant", {"plant": "PLT-02"}, {"customer_no": "C000031"}),
    ("A1", "otif_total", {}, {"plant": "PLT-02"}),
    ("A2", "fill_weight_plant", {"plant": "PLT-01"}, {"product_group": "CASE-READY"}),
])
def test_attribution_names_the_segment_the_answer_key_names(world, cfg: DetectConfig, anomaly_id: str,
                                                            grain: str, segment: dict, expect: dict) -> None:
    """The point of the whole module: from a coarse anomaly, land on the cause the generator seeded."""
    frames, series = world
    spec = _answer_key()[anomaly_id]
    span = (date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"]))
    anomaly = _worst(series, cfg, grain, spec["metric"], segment, span)

    result = attribute(frames, cfg, anomaly)
    assert result.drivers, "a coarse anomaly with finer data should name drivers"
    top = result.drivers[0]
    for key, value in expect.items():
        assert top.segment[key] == value, f"{anomaly_id}: top driver was {top.label}"
    assert top.rate_effect < 0                      # the driver got worse, not better
    assert top.evidence_refs and all(":" in ref for ref in top.evidence_refs)


def test_the_baseline_window_used_for_attribution_precedes_the_anomaly(world, cfg: DetectConfig) -> None:
    frames, series = world
    spec = _answer_key()["A1"]
    span = (date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"]))
    anomaly = _worst(series, cfg, "otif_plant", "otif_rate", {"plant": "PLT-02"}, span)
    result = attribute(frames, cfg, anomaly)

    assert result.baseline[1] < result.window[0]
    assert (result.baseline[1] - result.baseline[0]).days == cfg.baseline_days - 1
    assert result.window == (anomaly.window_start, anomaly.metric_date)


def test_attribution_is_deterministic_and_keyed_by_anomaly(world, cfg: DetectConfig) -> None:
    frames, series = world
    spec = _answer_key()["A1"]
    span = (date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"]))
    anomaly = _worst(series, cfg, "otif_plant", "otif_rate", {"plant": "PLT-02"}, span)

    once: dict[str, Attribution] = attribute_all(frames, cfg, [anomaly])
    twice = attribute_all(frames, cfg, [anomaly])
    assert list(once) == ["otif_rate:plant=PLT-02"]
    assert once["otif_rate:plant=PLT-02"].to_dict() == twice["otif_rate:plant=PLT-02"].to_dict()


def _window_anomaly(start: date, end: date) -> Anomaly:
    return Anomaly(metric="otif_rate", grain="otif_total", segment={}, metric_date=end, window_start=start,
                   severity=Severity.WARN, value=0.7, expected=0.9, delta=-0.2, direction="down",
                   volume=100.0, magnitude=0.2)


def test_attribution_never_cites_a_day_it_excluded(world, cfg: DetectConfig) -> None:
    """A window straddling the 4 July shutdown is explained by its trading days, and cites only those."""
    frames, _ = world
    result = attribute(frames, cfg, _window_anomaly(date(2026, 6, 29), date(2026, 7, 8)))

    assert result.drivers, "the trading days either side of the shutdown still carry the movement"
    cited = [date.fromisoformat(ref.split(":")[2]) for d in result.drivers for ref in d.evidence_refs]
    assert cited and not any(cfg.is_holiday(day) for day in cited)
    assert all(result.window[0] <= day <= result.window[1] for day in cited)


def test_a_window_entirely_inside_a_shutdown_attributes_nothing(world, cfg: DetectConfig) -> None:
    """2–5 July are all within the policy's holiday window, so there is no trading day left to explain.

    Naming a driver here would be blaming a lane for a day the plants were shut — precisely the A5 mistake.
    """
    frames, _ = world
    result = attribute(frames, cfg, _window_anomaly(date(2026, 7, 2), date(2026, 7, 5)))
    assert result.drivers == []
    assert result.note == "no finer segment moved"
