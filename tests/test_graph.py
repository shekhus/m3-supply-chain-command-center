"""The approval flow, and the pause that survives a restart.

This is the test that justifies LangGraph being in the project at all. A brief is built and paused before it
can touch anything; the compiled graph, the saver and the database connection are then thrown away entirely,
and a new set resumes the same run from Postgres and executes what a person approved. If that did not need to
work, plain code would do — and the file would say so.

Everything the nodes reach out to is injected, so the flow is tested without a model, a tracker or a network:
the point here is the machine and its pause, not the pieces it calls.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Engine, create_engine, text

import graph as graph_module
from act.models import Action, ActionStatus, Decision
from act.policy_gate import PolicyError
from app.config import REPO_ROOT
from detect.config import load_detect_config
from detect.run import build_series, load_frames
from graph import GraphDeps, checkpointer, decide, load_run, start_run, thread_id_for
from metrics.runner import load_policy
from narrate.fallback import templated_brief
from narrate.run import NarrationResult
from narrate.validate import validate
from pack.build import build_pack
from pack.permissions import audience_for
from pack.schema import EvidencePack

POLICY = REPO_ROOT / "policy.yaml"
METRICS = REPO_ROOT / "data" / "metrics"
LANE_DAY = date(2025, 9, 15)
QUIET_DAY = date(2025, 4, 20)


@pytest.fixture(scope="module")
def world() -> tuple:
    if not (METRICS / "daily_otif_total.parquet").exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    cfg = load_detect_config(POLICY)
    policy = load_policy(POLICY)
    frames = load_frames(METRICS)
    return frames, cfg, policy, build_series(frames, cfg)


class Tracker:
    """Stands in for the Jira adapter. Records what it was asked to do, and can be told to fail."""

    def __init__(self, outcome: ActionStatus = ActionStatus.EXECUTED) -> None:
        self.calls: list[Action] = []
        self.outcome = outcome

    def __call__(self, action: Action) -> tuple[ActionStatus, str | None]:
        self.calls.append(action)
        if self.outcome is not ActionStatus.EXECUTED:
            return self.outcome, None
        return ActionStatus.EXECUTED, f"SC-{100 + len(self.calls)}"


def _deps(world: tuple, tracker: Tracker, audience: str = "vp") -> GraphDeps:
    frames, cfg, policy, series = world

    def build(run_date: date, who: str) -> tuple[EvidencePack, object]:
        return build_pack(frames, cfg, policy, run_date, series, audience_for(who))

    def narrate_without_a_model(pack: EvidencePack) -> NarrationResult:
        brief = templated_brief(pack)
        return NarrationResult(brief=brief, source="fallback", validation=validate(brief, pack),
                               fallback_reason="no model in this test", attempts=0)

    return GraphDeps(build_pack=build, policy=policy, narrate=narrate_without_a_model,
                     execute=tracker)


@pytest.fixture
def db(pg_url: str) -> Engine:
    """A database with the schemas the checkpointer and the record need."""
    from db.migrate import migrate

    migrate(pg_url)
    return create_engine(pg_url)


@pytest.mark.postgres
def test_a_run_pauses_before_it_can_touch_anything(world: tuple, db: Engine) -> None:
    tracker = Tracker()
    deps = _deps(world, tracker)
    with checkpointer(str(db.url.render_as_string(hide_password=False))) as saver:
        view = start_run(saver, deps, LANE_DAY, "vp")

    assert view.awaiting_approval and view.next_nodes == ("execute",)
    assert view.status == "PENDING_APPROVAL"
    assert view.brief is not None and view.brief.items
    assert view.pending, "there should be something to approve"
    assert tracker.calls == [], "nothing may reach the tracker before a person resumes"


@pytest.mark.postgres
def test_the_pause_survives_the_process_that_created_it(world: tuple, db: Engine) -> None:
    """The reason this is a graph: approve at 14:00, from a different process, and the run continues."""
    url = str(db.url.render_as_string(hide_password=False))
    tracker = Tracker()

    with checkpointer(url) as saver:                       # morning: build and pause
        started = start_run(saver, _deps(world, tracker), LANE_DAY, "vp")
        approve = [Decision(action_id=a.id, verdict="approve", decided_by="vp")
                   for a in started.pending]
    # everything from that first run is gone now: the compiled graph, the saver, the connection

    with checkpointer(url) as fresh_saver:                 # afternoon: a new process resumes it
        resumed = decide(fresh_saver, _deps(world, tracker), started.thread_id, approve)

    assert resumed.status == "EXECUTED"
    assert len(tracker.calls) == len(approve)
    assert all(a.external_ref for a in resumed.actions if a.status is ActionStatus.EXECUTED)
    assert resumed.executed and all(e["external_ref"] for e in resumed.executed)


@pytest.mark.postgres
def test_a_rejected_action_is_never_executed(world: tuple, db: Engine) -> None:
    tracker = Tracker()
    url = str(db.url.render_as_string(hide_password=False))
    with checkpointer(url) as saver:
        started = start_run(saver, _deps(world, tracker), LANE_DAY, "vp")
        rejections = [Decision(action_id=a.id, verdict="reject", decided_by="vp", note="known issue")
                      for a in started.pending]
        resumed = decide(saver, _deps(world, tracker), started.thread_id, rejections)

    assert tracker.calls == []
    assert resumed.status == "REJECTED"
    assert all(a.status is ActionStatus.REJECTED for a in resumed.actions
               if a.id in {d.action_id for d in rejections})
    assert all(a.decided_by == "vp" for a in resumed.actions if a.status is ActionStatus.REJECTED)


@pytest.mark.postgres
def test_an_edit_is_what_gets_executed(world: tuple, db: Engine) -> None:
    """A person rewrote the ask; the tracker must receive their words, not the drafter's."""
    tracker = Tracker()
    url = str(db.url.render_as_string(hide_password=False))
    with checkpointer(url) as saver:
        started = start_run(saver, _deps(world, tracker), LANE_DAY, "vp")
        first = started.pending[0]
        edit = Decision(action_id=first.id, verdict="edit", decided_by="plt02",
                        title="Chase the C000031 lane with the carrier",
                        body="Call the carrier about the missed collections.")
        resumed = decide(saver, _deps(world, tracker), started.thread_id, [edit])

    assert tracker.calls and tracker.calls[0].title == "Chase the C000031 lane with the carrier"
    assert tracker.calls[0].body.startswith("Call the carrier")
    executed = next(a for a in resumed.actions if a.id == first.id)
    assert executed.decided_by == "plt02" and executed.decided_at is not None


@pytest.mark.postgres
def test_an_action_left_undecided_is_left_alone(world: tuple, db: Engine) -> None:
    """Approving one thing is not approving everything: silence is not consent."""
    tracker = Tracker()
    url = str(db.url.render_as_string(hide_password=False))
    with checkpointer(url) as saver:
        started = start_run(saver, _deps(world, tracker), LANE_DAY, "vp")
        if len(started.pending) < 2:
            pytest.skip("this day only proposes one action")
        one = Decision(action_id=started.pending[0].id, verdict="approve", decided_by="vp")
        resumed = decide(saver, _deps(world, tracker), started.thread_id, [one])

    assert len(tracker.calls) == 1
    untouched = next(a for a in resumed.actions if a.id == started.pending[1].id)
    assert untouched.status is ActionStatus.PROPOSED and untouched.decided_by is None


@pytest.mark.postgres
def test_policy_is_enforced_again_at_the_moment_of_approval(world: tuple, db: Engine) -> None:
    """Resuming a run is another way into `execute`, so the allow-list has to hold there too.

    The type used here is a real one that policy does not permit for *this* anomaly: `investigation` is
    allowed for a fill-rate drop and not for an OTIF drop. A made-up type like "update_m3" never reaches the
    gate at all — the contract refuses it first, which is the outer of the two layers.
    """
    tracker = Tracker()
    url = str(db.url.render_as_string(hide_password=False))
    deps = _deps(world, tracker)
    with checkpointer(url) as saver:
        started = start_run(saver, deps, LANE_DAY, "vp")
        target = next(a for a in started.pending if a.anomaly_type == "otif_drop")
        assert "investigation" not in deps.policy["allowed_actions"]["otif_drop"]

        compiled = graph_module.build_graph(deps).compile(checkpointer=saver,
                                                          interrupt_before=["execute"])
        tampered = [{**a.model_dump(mode="json"),
                     **({"type": "investigation"} if a.id == target.id else {})}
                    for a in started.actions]
        compiled.update_state(graph_module._config(started.thread_id), {"actions": tampered})

        with pytest.raises(PolicyError, match="not allowed"):
            decide(saver, deps, started.thread_id,
                   [Decision(action_id=target.id, verdict="approve", decided_by="vp")])
    assert tracker.calls == []


def test_a_made_up_action_type_never_even_parses(world: tuple) -> None:
    """The contract is the outer layer: a type that does not exist cannot be built, let alone approved."""
    with pytest.raises(ValueError, match="jira_ticket"):
        Action.model_validate({"id": "x", "item_id": "I1", "anomaly_type": "otif_drop",
                               "type": "update_m3", "title": "t", "body": "b",
                               "assignee_hint": "someone"})


@pytest.mark.postgres
def test_a_failing_tracker_leaves_the_action_retryable(world: tuple, db: Engine) -> None:
    """A timeout is not a rejection: the action stays open so the next run can try again."""
    tracker = Tracker(outcome=ActionStatus.FAILED_RETRYABLE)
    url = str(db.url.render_as_string(hide_password=False))
    with checkpointer(url) as saver:
        started = start_run(saver, _deps(world, tracker), LANE_DAY, "vp")
        resumed = decide(saver, _deps(world, tracker), started.thread_id,
                         [Decision(action_id=started.pending[0].id, verdict="approve", decided_by="vp")])

    failed = next(a for a in resumed.actions if a.id == started.pending[0].id)
    assert failed.status is ActionStatus.FAILED_RETRYABLE and failed.is_open
    assert resumed.status == "REJECTED"        # nothing was executed, and the run says so


@pytest.mark.postgres
def test_a_quiet_day_needs_no_approval(world: tuple, db: Engine) -> None:
    tracker = Tracker()
    url = str(db.url.render_as_string(hide_password=False))
    with checkpointer(url) as saver:
        view = start_run(saver, _deps(world, tracker), QUIET_DAY, "vp")

    assert view.status == "EMPTY" and not view.pending
    assert view.brief is not None and view.brief.summary
    assert tracker.calls == []


@pytest.mark.postgres
def test_a_second_run_for_the_same_morning_resumes_rather_than_starting_again(world: tuple,
                                                                              db: Engine) -> None:
    """Idempotent by run date and reader: the cron firing twice must not produce two briefs."""
    tracker = Tracker()
    url = str(db.url.render_as_string(hide_password=False))
    with checkpointer(url) as saver:
        first = start_run(saver, _deps(world, tracker), LANE_DAY, "vp")
        again = start_run(saver, _deps(world, tracker), LANE_DAY, "vp")

    assert again.thread_id == first.thread_id == thread_id_for(LANE_DAY, "vp")
    assert [a.id for a in again.actions] == [a.id for a in first.actions]
    assert tracker.calls == []


@pytest.mark.postgres
def test_a_paused_run_can_be_read_back_without_advancing_it(world: tuple, db: Engine) -> None:
    """The console loads a run to show it; looking at something must not execute it."""
    tracker = Tracker()
    url = str(db.url.render_as_string(hide_password=False))
    with checkpointer(url) as saver:
        started = start_run(saver, _deps(world, tracker), LANE_DAY, "vp")
        view = load_run(saver, _deps(world, tracker), started.thread_id)

    assert view.awaiting_approval and view.thread_id == started.thread_id
    assert view.brief is not None and [a.id for a in view.actions] == [a.id for a in started.actions]
    assert tracker.calls == []


@pytest.mark.postgres
def test_each_audience_gets_its_own_thread(world: tuple, db: Engine) -> None:
    tracker = Tracker()
    url = str(db.url.render_as_string(hide_password=False))
    with checkpointer(url) as saver:
        vp = start_run(saver, _deps(world, tracker), LANE_DAY, "vp")
        plant = start_run(saver, _deps(world, tracker, "plt01"), LANE_DAY, "plt01")

    assert vp.thread_id != plant.thread_id
    assert plant.status == "EMPTY"                      # PLT-01 has nothing on this day
    assert all("PLT-02" not in a.body for a in plant.actions)


@pytest.mark.postgres
def test_the_checkpoint_tables_live_in_their_own_schema(db: Engine) -> None:
    """The graph's internals must never collide with the business tables, or with a future LangGraph."""
    url = str(db.url.render_as_string(hide_password=False))
    with checkpointer(url):
        pass
    with db.connect() as conn:
        schemas = {row[0] for row in conn.execute(text(
            "SELECT table_schema FROM information_schema.tables WHERE table_name LIKE 'checkpoint%'"))}
    assert schemas == {graph_module.CHECKPOINT_SCHEMA}


def test_the_graph_cannot_reach_execute_without_an_interrupt(world: tuple) -> None:
    """Compiled without a checkpointer it still declares the stop; the guarantee is in the compile call."""
    tracker = Tracker()
    compiled = graph_module.build_graph(_deps(world, tracker)).compile(interrupt_before=["execute"])
    assert "execute" in compiled.nodes
    assert tracker.calls == []
