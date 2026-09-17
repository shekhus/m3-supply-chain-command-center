"""The eight failure cases from the plan (§3.6), each one demonstrated rather than asserted about.

These are the cases the project exists to survive, so they are tested where a reader can find them: one test
per row of the table, named after the failure, with the expected behaviour in the docstring. Several of them
are covered in more detail elsewhere — this file is the index, and the place a sceptical reader is sent.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import Engine, create_engine, text

from act.draft import draft_actions
from act.jira_adapter import Executor, NoteOnly, TrackerRefused, TrackerTimeout
from act.models import Action, ActionStatus, OpenAction
from act.policy_gate import evaluate
from app.config import REPO_ROOT
from detect.config import load_detect_config
from detect.run import build_series, load_frames
from metrics.runner import load_policy
from narrate.contract import Brief, BriefItem, Claim
from narrate.fallback import templated_brief
from narrate.run import narrate
from narrate.validate import validate
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


# 1 --------------------------------------------------------------------------------------------


def test_stale_gold_is_disclosed_not_narrated_over(world: tuple) -> None:
    """Expected: the brief opens with how old the data is, and affected items are marked."""
    frames, cfg, policy, series = world
    stale_day = max(frames["daily_otif_total"]["metric_date"]) + timedelta(days=4)
    stale_pack, _ = build_pack(frames, cfg, policy, stale_day, series, audience_for("vp"))

    assert stale_pack.any_stale
    brief = templated_brief(stale_pack)
    assert brief.freshness_line and "Stale" in brief.freshness_line
    assert validate(brief, stale_pack).ok

    silent = Brief(summary="All fine.", freshness_line=None, items=[])
    assert any(c.rule == "freshness-missing" for c in validate(silent, stale_pack).complaints)


# 2 --------------------------------------------------------------------------------------------


def test_invalid_json_from_the_model_falls_back(pack: EvidencePack) -> None:
    """Expected: retry once, then the templated brief, with `fallback_reason` logged."""
    from test_narrate import _client

    client, _backend, recorder = _client("not json", "still not json")
    result = narrate(pack, client)

    assert result.source == "fallback" and result.validation.ok
    assert "model failed twice" in (result.fallback_reason or "")
    assert [c.outcome for c in recorder.calls] == ["invalid_output", "invalid_output"]


# 3 --------------------------------------------------------------------------------------------


def test_a_metric_ref_that_is_not_in_the_pack_is_rejected(pack: EvidencePack) -> None:
    """Expected: the validator rejects it, the model is asked again, and the template ships if it fails."""
    invented = Brief(summary="s", freshness_line="Data is current.", items=[BriefItem(
        id=pack.items[0].id, headline="OTIF fell", why="It fell.", severity=pack.items[0].severity,
        claims=[Claim(text="It fell to 12.3%.", metric_ref="I42.value")])])

    result = validate(invented, pack)
    assert not result.ok
    assert any(c.rule == "unknown-ref" for c in result.complaints)


# 4 --------------------------------------------------------------------------------------------


def test_a_jira_timeout_is_retryable_and_not_an_exception() -> None:
    """Expected: the action becomes FAILED_RETRYABLE, the console shows it, the next run retries."""
    class Slow:
        def create(self, action: Action) -> str:
            raise TrackerTimeout("no answer in 10s")

    action = Action(id="a1", item_id="I1", anomaly_type="otif_drop", type="jira_ticket",
                    title="t", body="b", assignee_hint="someone")
    status, ref = Executor(tracker=Slow(), notes=NoteOnly())(action)

    assert status is ActionStatus.FAILED_RETRYABLE and ref is None
    assert action.with_status(status).is_open, "it must still be open for the next run to pick up"


def test_a_tracker_refusal_needs_a_person_rather_than_a_retry_loop() -> None:
    class Refusing:
        def create(self, action: Action) -> str:
            raise TrackerRefused("400: no such project")

    action = Action(id="a2", item_id="I1", anomaly_type="otif_drop", type="jira_ticket",
                    title="t", body="b", assignee_hint="someone")
    status, _ = Executor(tracker=Refusing(), notes=NoteOnly())(action)
    assert status is ActionStatus.FAILED


# 5 --------------------------------------------------------------------------------------------


def test_a_plant_manager_sees_only_their_plant(world: tuple) -> None:
    """Expected: leadership items are *absent from their pack*, not hidden in the UI."""
    import json

    frames, cfg, policy, series = world
    theirs, redaction = build_pack(frames, cfg, policy, LANE_DAY, series, audience_for("plt01"))

    assert redaction.removed > 0
    serialised = json.dumps(theirs.for_prompt())
    assert "PLT-02" not in serialised and "C000031" not in serialised
    assert all(item.segment.get("plant") == "PLT-01" for item in theirs.items)


# 6 --------------------------------------------------------------------------------------------


def test_an_action_outside_policy_is_blocked_before_approval(pack: EvidencePack, world: tuple) -> None:
    """Expected: blocked in code before anybody is asked, and recorded as a policy violation."""
    policy = world[2]
    smuggled = draft_actions(pack, policy)[0].model_copy(update={"type": "update_m3"})

    result = evaluate([smuggled], pack, policy)
    assert not result.allowed and result.blocked
    assert result.blocked[0].status is ActionStatus.BLOCKED
    assert result.violations and "not allowed" in result.violations[0]


# 7 --------------------------------------------------------------------------------------------


def test_a_metric_missing_for_the_day_produces_no_invented_number(world: tuple) -> None:
    """Expected: the item is skipped rather than filled in; the brief never invents a figure."""
    frames, cfg, policy, series = world
    empty_day = min(frames["daily_otif_total"]["metric_date"]) - timedelta(days=5)
    quiet, _ = build_pack(frames, cfg, policy, empty_day, series, audience_for("vp"))

    assert quiet.is_quiet and quiet.quiet_reason
    brief = templated_brief(quiet)
    assert brief.items == []
    assert validate(brief, quiet).ok


# 8 --------------------------------------------------------------------------------------------


@pytest.fixture
def db(pg_url: str) -> Engine:
    from db.migrate import migrate

    migrate(pg_url)
    return create_engine(pg_url)


@pytest.mark.postgres
def test_a_duplicate_run_for_the_same_date_is_idempotent(db: Engine, world: tuple) -> None:
    """Expected: the second run returns the first — one morning, one brief, whatever the cron does."""
    from act import store

    first = store.start_run(db, LANE_DAY, "vp")
    second = store.start_run(db, LANE_DAY, "vp")
    assert first == second

    with db.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM ops.runs WHERE run_date = :d AND audience = 'vp'"),
                             {"d": LANE_DAY}).scalar_one()
    assert count == 1


def test_a_second_ticket_for_an_open_problem_is_suppressed(pack: EvidencePack, world: tuple) -> None:
    """Expected: the same lane does not raise a ticket every morning until somebody mutes the system."""
    policy = world[2]
    actions = draft_actions(pack, policy)
    item = next(i for i in pack.items if i.id == actions[0].item_id)
    already = OpenAction(anomaly_type=actions[0].anomaly_type, segment_label=item.segment_label,
                         metric=item.metric, type=actions[0].type, created_on=pack.run_date,
                         external_ref="SC-0001")

    result = evaluate(actions, pack, policy, [already])
    assert actions[0].id in {a.id for a in result.suppressed}
    assert "SC-0001" in (result.suppressed[0].blocked_reason or "")


# the promise underneath all eight ----------------------------------------------------------------


def test_the_brief_always_ships(pack: EvidencePack) -> None:
    """Whatever fails, something correct goes out: a day with no brief is a silent failure."""
    from test_narrate import _client

    for answers in (("not json", "not json"),
                    ('{"summary":"x","freshness_line":"","items":[{"id":"I99","headline":"h",'
                     '"why":"w","severity":"HIGH","claims":[{"text":"t","metric_ref":"I99.value"}]}]}',) * 2):
        client, _b, _r = _client(*answers)
        result = narrate(pack, client)
        assert result.brief is not None and result.brief.summary
        assert result.validation.ok, "whatever ships has passed the gate"

    assert narrate(pack, None).source == "fallback"


def test_nothing_in_this_system_writes_to_the_erp() -> None:
    """Principle 8, checked rather than asserted in prose: no module writes anywhere near M3."""
    banned = ("m3.", "ion_api", "erp_write", "post_to_m3")
    for path in sorted(REPO_ROOT.glob("**/*.py")):
        if ".venv" in path.parts or "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8").lower()
        assert not any(term in source for term in banned), path
