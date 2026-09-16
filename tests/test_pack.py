"""The evidence pack and the permission filter.

Two promises are made here that everything downstream depends on. First: every number a brief could say exists
in the pack as a named fact with its rounding already done — so the validator in week 8 can check a claim
without recomputing anything. Second: a reader's pack contains only what that reader may see, because it was
built that way, not because a renderer hid the rest.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.config import REPO_ROOT
from detect.config import load_detect_config
from detect.run import build_series, load_frames
from metrics.runner import load_policy
from pack.build import build_pack
from pack.permissions import DIRECTORY, Audience, PermissionError_, audience_for, plants_for
from pack.schema import EvidencePack

POLICY = REPO_ROOT / "policy.yaml"
METRICS = REPO_ROOT / "data" / "metrics"
LANE_DAY = date(2025, 9, 15)      # A1's lane collapse, visible at PLT-02
HOLIDAY = date(2026, 7, 4)
QUIET_DAY = date(2025, 4, 20)


@pytest.fixture(scope="module")
def world() -> tuple:
    if not (METRICS / "daily_otif_total.parquet").exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    cfg = load_detect_config(POLICY)
    policy = load_policy(POLICY)
    frames = load_frames(METRICS)
    return frames, cfg, policy, build_series(frames, cfg)


def _pack(world: tuple, day: date = LANE_DAY, user: str = "vp") -> EvidencePack:
    frames, cfg, policy, series = world
    pack, _redaction = build_pack(frames, cfg, policy, day, series, audience_for(user))
    return pack


# --- every number is a named, rounded fact ----------------------------------------------------


def test_every_reference_an_item_carries_resolves_to_a_fact(world: tuple) -> None:
    """The validator will trust this: a ref in the pack always has a number behind it."""
    pack = _pack(world)
    assert pack.items
    for item in pack.items:
        refs = [item.value_ref, item.volume_ref, item.expected_ref, item.delta_ref]
        refs += [r for d in item.drivers for r in (d.share_ref, d.value_ref, d.baseline_ref)]
        for ref in [r for r in refs if r]:
            assert pack.fact(ref) is not None, f"{ref} has no fact"
    for source in pack.freshness:
        assert pack.fact(source.days_ref) is not None


def test_rounding_happens_once_in_code_and_the_display_matches_the_value(world: tuple) -> None:
    """A model asked to turn 0.74138 into a percentage is sometimes wrong; it is asked to copy instead."""
    pack = _pack(world)
    for fact in pack.facts.values():
        assert fact.display
        if fact.unit == "rate":
            assert fact.display == f"{fact.value * 100:.1f}%"
        if fact.unit == "pounds":
            assert fact.display.endswith(" lb")


def test_a_rate_change_is_spoken_of_in_points_and_a_variance_is_not_multiplied(world: tuple) -> None:
    """Two things called "points" that are not the same thing: 0.5 points of yield is not 50.0 points."""
    pack = _pack(world)
    changes = [f for f in pack.facts.values() if f.unit == "rate_change"]
    assert changes and all(f.display.endswith(" points") for f in changes)
    for fact in changes:
        assert fact.display == f"{fact.value * 100:.1f} points"

    yield_pack = _pack(world, date(2026, 1, 14))
    variance = [f for f in yield_pack.facts.values()
                if f.unit == "points" and f.metric == "yield_variance_pct"]
    assert variance and all(abs(f.value) < 20 for f in variance)
    for fact in variance:
        assert fact.display == f"{fact.value:.1f} points"


def test_the_prompt_view_contains_no_number_that_is_not_a_fact(world: tuple) -> None:
    """Principle 1, structurally: the model gets labels, refs and the facts it may quote. Nothing else."""
    pack = _pack(world)
    view = pack.for_prompt()
    assert set(view["facts"]) == set(pack.facts)
    assert all(set(f) == {"value", "means"} for f in view["facts"].values())
    for item in view["items"]:
        assert "series" not in item          # a series invites averaging; the pack does the arithmetic
        assert "rank" not in item and "volume_scale" not in item
        for ref in item["refs"].values():
            assert ref in view["facts"]
    assert json.dumps(view)                  # it has to survive the trip to a prompt


def test_an_item_says_why_it_was_flagged_in_words_a_person_can_check(world: tuple) -> None:
    pack = _pack(world)
    item = pack.items[0]
    assert item.detectors and all(note.detail for note in item.detectors)
    assert any(note.detector == "threshold" for note in item.detectors) or item.severity == "WARN"
    assert item.segment_label and item.metric_label != item.metric


def test_the_seeded_lane_collapse_arrives_in_the_pack_with_its_driver(world: tuple) -> None:
    """A1 end to end: the plant-level item names the lane, with a share that came from the decomposition."""
    pack = _pack(world)
    plant_item = next(i for i in pack.items if i.grain == "otif_plant" and i.metric == "otif_rate")
    assert plant_item.drivers
    top = plant_item.drivers[0]
    assert top.segment.get("customer_no") == "C000031"
    assert pack.fact(top.share_ref) is not None
    assert top.share_pct > 0                  # it is a share of the drop, stated positively


def test_freshness_is_always_disclosed_even_when_nothing_is_stale(world: tuple) -> None:
    """Principle 3: the brief says how old the data is whether or not there is a problem with it."""
    pack = _pack(world)
    assert pack.freshness and len(pack.freshness) >= 2
    assert all(source.latest <= pack.run_date for source in pack.freshness)
    assert pack.any_stale == any(s.stale for s in pack.freshness)
    assert "data_freshness" in pack.for_prompt()


def test_a_holiday_is_named_before_the_numbers_are(world: tuple) -> None:
    """Otherwise the 4 July brief opens with OTIF at 34.6% and no explanation."""
    pack = _pack(world, HOLIDAY)
    assert pack.is_holiday and pack.calendar_note
    assert "holiday" in pack.calendar_note
    assert pack.for_prompt()["calendar_note"] == pack.calendar_note
    assert all(item.severity == "WARN" for item in pack.items)   # and nothing became an incident


def test_a_quiet_day_says_which_kind_of_quiet_it_was(world: tuple) -> None:
    """"Nothing happened" and "nothing you can see happened" are different mornings."""
    quiet = _pack(world, QUIET_DAY)
    assert quiet.is_quiet and quiet.quiet_reason
    assert "material" in quiet.quiet_reason

    elsewhere = _pack(world, LANE_DAY, user="plt01")
    assert elsewhere.is_quiet
    assert elsewhere.quiet_reason == "nothing to report for the plants in this brief"


def test_the_policy_cap_limits_what_a_brief_may_carry(world: tuple) -> None:
    frames, cfg, policy, series = world
    pack, _ = build_pack(frames, cfg, policy, HOLIDAY, series, audience_for("vp"))
    assert len(pack.items) <= pack.items_cap == policy["limits"]["max_items_per_brief"]
    assert pack.items_considered >= len(pack.items)
    assert [i.id for i in pack.items] == [f"I{n}" for n in range(1, len(pack.items) + 1)]


# --- the permission filter --------------------------------------------------------------------


def test_a_plant_managers_pack_never_contains_another_plant(world: tuple) -> None:
    """Absent from the pack, not hidden in the console: the model is never given the other plant at all."""
    frames, cfg, policy, series = world
    pack, redaction = build_pack(frames, cfg, policy, LANE_DAY, series, audience_for("plt01"))

    assert all(item.segment.get("plant") == "PLT-01" for item in pack.items)
    assert redaction.removed > 0 and "PLT-02" in " ".join(redaction.segments)
    serialised = json.dumps(pack.for_prompt())
    assert "PLT-02" not in serialised
    assert "C000031" not in serialised


def test_the_plant_that_owns_the_incident_still_sees_it(world: tuple) -> None:
    pack = _pack(world, LANE_DAY, user="plt02")
    assert pack.items and all(i.segment.get("plant") == "PLT-02" for i in pack.items)


def test_a_regional_manager_sees_their_plants_and_no_others(world: tuple) -> None:
    east = audience_for("east")
    assert east.may_see({"plant": "PLT-01"}) and east.may_see({"plant": "PLT-02"})
    assert not east.may_see({"plant": "PLT-03"})
    assert not east.may_see({})                       # a company total is nobody's to act on
    assert not east.may_see({"location": "DC-EAST"})


def test_leadership_sees_everything_including_what_belongs_to_no_plant(world: tuple) -> None:
    vp = audience_for("vp")
    assert vp.sees_everything and plants_for(vp) is None
    assert vp.may_see({}) and vp.may_see({"plant": "PLT-03"}) and vp.may_see({"location": "DC-EAST"})


def test_an_unknown_user_is_refused_rather_than_given_an_empty_brief(world: tuple) -> None:
    """An empty brief looks like a quiet morning; that is how a broken permission check hides itself."""
    with pytest.raises(PermissionError_, match="no audience is configured"):
        audience_for("nobody")


def test_a_role_with_no_plants_is_not_accidentally_unrestricted() -> None:
    """`plants=()` means "everything" only for a role that is meant to see everything."""
    stranger = Audience(user="x", role="plant_manager", plants=())
    assert not stranger.sees_everything
    assert not stranger.may_see({"plant": "PLT-01"}) and not stranger.may_see({})


def test_every_configured_audience_is_coherent() -> None:
    for user, audience in DIRECTORY.items():
        assert audience.user == user
        assert audience.sees_everything or audience.plants
        assert all(p.startswith("PLT-") for p in audience.plants)
