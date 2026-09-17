"""Action drafting and the policy gate.

The gate is tested the way it will be attacked: with actions nobody's drafter would produce. A type the policy
does not list, an anomaly type it has never heard of, an action about an item that is not in the brief, a
second ticket for something already open, and an approval of something the gate already refused. Each is
blocked in code, before a person is asked, and recorded as a violation rather than quietly dropped.

The drafter is tested for the other half of the promise: a ticket read next month, without the brief beside
it, still says where every number came from.
"""

from __future__ import annotations

from datetime import date

import pytest

from act.draft import ASSIGNEES, draft_actions, key_for
from act.models import Action, ActionStatus, Decision, OpenAction
from act.policy_gate import PolicyError, check_decision, evaluate, load_gate_policy
from app.config import REPO_ROOT
from detect.config import load_detect_config
from detect.run import build_series, load_frames
from metrics.runner import load_policy
from pack.build import build_pack
from pack.permissions import audience_for
from pack.schema import EvidencePack

POLICY = REPO_ROOT / "policy.yaml"
METRICS = REPO_ROOT / "data" / "metrics"
LANE_DAY = date(2025, 9, 15)


@pytest.fixture(scope="module")
def world() -> tuple:
    if not (METRICS / "daily_otif_total.parquet").exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    cfg = load_detect_config(POLICY)
    policy = load_policy(POLICY)
    frames = load_frames(METRICS)
    return frames, cfg, policy, build_series(frames, cfg)


@pytest.fixture(scope="module")
def pack(world: tuple) -> EvidencePack:
    frames, cfg, policy, series = world
    built, _ = build_pack(frames, cfg, policy, LANE_DAY, series, audience_for("vp"))
    return built


@pytest.fixture(scope="module")
def policy(world: tuple) -> dict:
    return world[2]


def _actions(pack: EvidencePack, policy: dict) -> list[Action]:
    return draft_actions(pack, policy)


# --- drafting ---------------------------------------------------------------------------------


def test_each_item_gets_one_action_of_a_type_the_policy_allows(pack: EvidencePack, policy: dict) -> None:
    actions = _actions(pack, policy)
    assert actions and len(actions) <= policy["limits"]["max_actions_per_brief"]
    assert [a.item_id for a in actions] == [i.id for i in pack.items][:len(actions)]
    for action in actions:
        assert action.type in policy["allowed_actions"][action.anomaly_type]
        assert action.status is ActionStatus.PROPOSED
        assert action.external_ref is None and action.decided_by is None


def test_a_high_item_gets_the_policys_first_choice_and_a_warn_gets_a_quieter_one(pack: EvidencePack,
                                                                                policy: dict) -> None:
    """Nobody wants a ticket for every WARN, and nobody wants a HIGH arriving as a note read on Thursday."""
    actions = {a.item_id: a for a in _actions(pack, policy)}
    for item in pack.items:
        action = actions.get(item.id)
        if action is None:
            continue
        allowed = policy["allowed_actions"][item.anomaly_type]
        if item.severity == "HIGH":
            assert action.type == allowed[0]
        else:
            assert action.type in ("internal_update", "investigation") or action.type == allowed[0]


def test_a_ticket_says_where_every_number_came_from(pack: EvidencePack, policy: dict) -> None:
    """It will be read next month without the brief beside it."""
    action = _actions(pack, policy)[0]
    item = next(i for i in pack.items if i.id == action.item_id)

    assert pack.facts[item.value_ref].display in action.body
    assert item.value_ref in action.body                      # the ref, so a reader can check it
    assert item.segment_label in action.body and item.window_end.isoformat() in action.body
    assert all(ref in action.body for ref in action.evidence_refs if ref.endswith((".expected", ".change")))
    assert action.evidence_refs and all(pack.fact(ref) for ref in action.evidence_refs)
    assert "computed in code" in action.body


def test_a_ticket_repeats_the_reason_the_detector_gave(pack: EvidencePack, policy: dict) -> None:
    action = _actions(pack, policy)[0]
    item = next(i for i in pack.items if i.id == action.item_id)
    assert item.detectors[0].detail in action.body


def test_a_driver_reaches_the_ticket_so_the_reader_knows_where_to_look(pack: EvidencePack,
                                                                      policy: dict) -> None:
    item = next(i for i in pack.items if i.drivers)
    action = next(a for a in _actions(pack, policy) if a.item_id == item.id)
    driver = item.drivers[0]
    assert driver.label in action.body
    assert pack.facts[driver.share_ref].display in action.body
    assert driver.share_ref in action.evidence_refs


def test_stale_data_is_disclosed_in_the_ticket_too(pack: EvidencePack, policy: dict) -> None:
    """A ticket outlives the brief, so the caveat has to travel with it.

    The case is constructed rather than found: a day whose metrics are stale has no metrics for that day, so
    it has no items either. Staleness reaching a *ticket* is therefore a shape the generated world cannot
    produce, and skipping the test would leave the path untested rather than proven.
    """
    stale_items = [item.model_copy(update={"stale": True}) for item in pack.items]
    stale_pack = pack.model_copy(update={"items": stale_items, "any_stale": True})

    body = draft_actions(stale_pack, policy)[0].body
    assert "stale" in body
    assert "check the dates before acting" in body


def test_an_anomaly_type_the_policy_has_no_action_for_gets_no_action(pack: EvidencePack,
                                                                     policy: dict) -> None:
    narrowed = {**policy, "allowed_actions": {k: v for k, v in policy["allowed_actions"].items()
                                              if k != "otif_drop"}}
    actions = draft_actions(pack, narrowed)
    assert all(a.anomaly_type != "otif_drop" for a in actions)


def test_every_anomaly_type_the_pack_can_produce_has_an_assignee() -> None:
    from pack.build import METRIC_LABELS
    for _label, _unit, anomaly_type in METRIC_LABELS.values():
        assert anomaly_type in ASSIGNEES, f"{anomaly_type} has nobody to send it to"


# --- the gate ---------------------------------------------------------------------------------


def test_a_clean_set_of_actions_passes_and_stays_proposed(pack: EvidencePack, policy: dict) -> None:
    result = evaluate(_actions(pack, policy), pack, policy)
    assert result.allowed and not result.blocked and not result.violations
    assert all(a.status is ActionStatus.PROPOSED for a in result.allowed)
    assert not any(a.status is ActionStatus.APPROVED for a in result.all_actions)


def test_an_action_type_outside_the_policy_is_blocked_before_anyone_is_asked(pack: EvidencePack,
                                                                            policy: dict) -> None:
    """The plan's failure case: "adjust price", "update M3". Blocked in code, logged as a violation."""
    action = _actions(pack, policy)[0]
    smuggled = action.model_copy(update={"type": "update_m3"})   # a type no drafter here can produce

    result = evaluate([smuggled], pack, policy)
    assert not result.allowed
    assert result.blocked and result.blocked[0].status is ActionStatus.BLOCKED
    assert "not allowed" in (result.blocked[0].blocked_reason or "")
    assert result.violations and smuggled.id in result.violations[0]


def test_an_action_for_an_anomaly_type_the_policy_never_heard_of_is_blocked(pack: EvidencePack,
                                                                           policy: dict) -> None:
    action = _actions(pack, policy)[0].model_copy(update={"anomaly_type": "price_change"})
    result = evaluate([action], pack, policy)
    assert result.blocked and "allows no actions" in (result.blocked[0].blocked_reason or "")
    assert result.violations


def test_an_action_about_an_item_that_is_not_in_the_brief_is_blocked(pack: EvidencePack,
                                                                    policy: dict) -> None:
    """Evidence the reader cannot see is evidence nobody can check."""
    action = _actions(pack, policy)[0].model_copy(update={"item_id": "I99"})
    result = evaluate([action], pack, policy)
    assert result.blocked and "no item I99" in (result.blocked[0].blocked_reason or "")


def test_a_blocked_action_is_kept_with_its_reason_rather_than_dropped(pack: EvidencePack,
                                                                     policy: dict) -> None:
    """The console shows what the system wanted to do and was refused; a silent drop hides policy drift."""
    action = _actions(pack, policy)[0].model_copy(update={"type": "update_m3"})
    result = evaluate([action], pack, policy)
    assert len(result.all_actions) == 1
    assert result.to_dict()["blocked"][0]["reason"]


def test_a_second_ticket_for_something_already_open_is_suppressed(pack: EvidencePack,
                                                                  policy: dict) -> None:
    actions = _actions(pack, policy)
    item = next(i for i in pack.items if i.id == actions[0].item_id)
    already = OpenAction(anomaly_type=actions[0].anomaly_type, segment_label=item.segment_label,
                         metric=item.metric, type=actions[0].type,
                         created_on=pack.run_date, external_ref="SC-114")

    result = evaluate(actions, pack, policy, [already])
    assert actions[0].id in {a.id for a in result.suppressed}
    assert "SC-114" in (result.suppressed[0].blocked_reason or "")
    assert len(result.allowed) == len(actions) - 1


def test_an_old_closed_ticket_does_not_suppress_a_new_one(pack: EvidencePack, policy: dict) -> None:
    """Suppression has a window for a reason: the same lane failing again next month is news."""
    from datetime import timedelta
    actions = _actions(pack, policy)
    item = next(i for i in pack.items if i.id == actions[0].item_id)
    window = int(policy["limits"]["suppress_if_open_ticket_days"])
    stale = OpenAction(anomaly_type=actions[0].anomaly_type, segment_label=item.segment_label,
                       metric=item.metric, type=actions[0].type,
                       created_on=pack.run_date - timedelta(days=window + 1), external_ref="SC-001")
    rejected = OpenAction(anomaly_type=actions[0].anomaly_type, segment_label=item.segment_label,
                          metric=item.metric, type=actions[0].type, created_on=pack.run_date,
                          status=ActionStatus.REJECTED)

    assert actions[0].id in {a.id for a in evaluate(actions, pack, policy, [stale]).allowed}
    assert actions[0].id in {a.id for a in evaluate(actions, pack, policy, [rejected]).allowed}


def test_one_brief_does_not_propose_the_same_thing_twice(pack: EvidencePack, policy: dict) -> None:
    action = _actions(pack, policy)[0]
    twin = action.model_copy(update={"id": action.id + "-again"})
    result = evaluate([action, twin], pack, policy)
    assert len(result.allowed) == 1 and len(result.suppressed) == 1


def test_the_policy_cap_stops_a_person_being_handed_fifteen_approvals(pack: EvidencePack,
                                                                      policy: dict) -> None:
    actions = _actions(pack, policy)
    many = [a.model_copy(update={"id": f"{a.id}-{n}", "item_id": pack.items[n % len(pack.items)].id,
                                 "anomaly_type": pack.items[n % len(pack.items)].anomaly_type})
            for n, a in enumerate(actions * 4)]
    tight = {**policy, "limits": {**policy["limits"], "max_actions_per_brief": 2}}

    result = evaluate(many, pack, tight)
    assert len(result.allowed) <= 2
    assert any("limit of 2" in (a.blocked_reason or "") for a in result.suppressed)


def test_the_gate_never_approves_anything(pack: EvidencePack, policy: dict) -> None:
    """Principle 5, structurally: there is no path through this module that approves or executes."""
    result = evaluate(_actions(pack, policy), pack, policy)
    assert {a.status for a in result.all_actions} <= {ActionStatus.PROPOSED, ActionStatus.BLOCKED,
                                                      ActionStatus.SUPPRESSED}


# --- approving --------------------------------------------------------------------------------


def test_approving_is_still_bound_by_the_policy(pack: EvidencePack, policy: dict) -> None:
    """An editor who changes an action's type must not walk past the allow-list on the way out."""
    action = _actions(pack, policy)[0]
    check_decision(action, "approve", policy)              # the drafted action is fine

    edited = action.model_copy(update={"type": "update_m3"})
    with pytest.raises(PolicyError, match="not allowed"):
        check_decision(edited, "edit", policy)


def test_something_the_gate_blocked_cannot_be_approved_afterwards(pack: EvidencePack,
                                                                  policy: dict) -> None:
    blocked = _actions(pack, policy)[0].with_status(ActionStatus.BLOCKED, blocked_reason="not allowed")
    with pytest.raises(PolicyError, match="blocked by policy"):
        check_decision(blocked, "approve", policy)


def test_rejecting_is_always_allowed(pack: EvidencePack, policy: dict) -> None:
    """A person may always say no, including to something policy would not have permitted."""
    odd = _actions(pack, policy)[0].model_copy(update={"type": "update_m3"})
    check_decision(odd, "reject", policy)


def test_an_unknown_verdict_is_refused(pack: EvidencePack, policy: dict) -> None:
    with pytest.raises(PolicyError, match="unknown verdict"):
        check_decision(_actions(pack, policy)[0], "maybe", policy)


def test_a_decision_records_who_and_what_they_changed() -> None:
    decision = Decision(action_id="a1", verdict="edit", decided_by="vp", title="Reworded",
                        body="New body", note="narrowed the ask")
    assert decision.verdict == "edit" and decision.decided_by == "vp"
    assert decision.title and decision.body


# --- the policy file itself -------------------------------------------------------------------


def test_the_repo_policy_is_coherent(policy: dict) -> None:
    allowed = load_gate_policy(policy)
    assert set(allowed) >= {"otif_drop", "fill_rate_drop", "yield_variance", "inventory_at_risk",
                            "backlog_build"}
    assert all(t in {"jira_ticket", "investigation", "internal_update"}
               for types in allowed.values() for t in types)


def test_a_policy_naming_an_unknown_action_type_is_an_error(policy: dict) -> None:
    """A typo in the allow-list must not read as "this is not allowed" — it is a broken file."""
    broken = {**policy, "allowed_actions": {"otif_drop": ["jira_tickets"]}}
    with pytest.raises(PolicyError, match="unknown action types"):
        load_gate_policy(broken)


def test_an_empty_allow_list_is_an_error(policy: dict) -> None:
    with pytest.raises(PolicyError, match="missing or empty"):
        load_gate_policy({**policy, "allowed_actions": {}})
    with pytest.raises(PolicyError, match="non-empty list"):
        load_gate_policy({**policy, "allowed_actions": {"otif_drop": []}})


def test_the_suppression_key_is_the_same_problem_not_the_same_wording(pack: EvidencePack,
                                                                      policy: dict) -> None:
    action = _actions(pack, policy)[0]
    reworded = action.model_copy(update={"title": "Something else entirely", "body": "different"})
    assert key_for(action, pack) == key_for(reworded, pack)
