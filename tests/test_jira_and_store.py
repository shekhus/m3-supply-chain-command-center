"""The adapter that leaves the building, and the record that outlives the run.

The adapter is tested for its failures rather than its successes: a timeout, a 500, a 4xx and an unexpected
exception each have to become a *status the next run can act on*, never an exception in the face of somebody
who approved something and walked away.

The store is tested for the two things that make it worth having: duplicate suppression works against what is
actually in the database rather than a list in memory, and a decision row survives the action being updated
afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import Engine, create_engine, text

from act import store
from act.jira_adapter import (
    Executor,
    LiveJira,
    NoteOnly,
    TrackerRefused,
    TrackerTimeout,
    build_executor,
)
from act.models import Action, ActionStatus, Decision
from db.migrate import migrate

LANE_DAY = date(2025, 9, 15)


def _action(action_id: str = "a1", action_type: str = "jira_ticket") -> Action:
    return Action(id=action_id, item_id="I1", anomaly_type="otif_drop", type=action_type,  # type: ignore[arg-type]
                  title="[HIGH] OTIF down to 50.0% at PLT-02 C000031",
                  body="OTIF at PLT-02 C000031 was 50.0% (I1.value).",
                  assignee_hint="plant operations lead", evidence_refs=["I1.value"])


@pytest.fixture
def db(pg_url: str) -> Engine:
    migrate(pg_url)
    return create_engine(pg_url)


# --- the adapter's failures --------------------------------------------------------------------


@dataclass
class Breaking:
    """A tracker that fails the way a real one does."""

    error: Exception

    def create(self, action: Action) -> str:
        raise self.error


def test_a_timeout_leaves_the_action_retryable() -> None:
    """The person approved it and left. A tracker being down is the system's problem, not theirs."""
    executor = Executor(tracker=Breaking(TrackerTimeout("no answer in 10s")), notes=NoteOnly())
    status, ref = executor(_action())
    assert status is ActionStatus.FAILED_RETRYABLE and ref is None


def test_a_refusal_is_not_retried_forever() -> None:
    """A 4xx means the request was wrong; retrying a wrong request is how a queue fills with failures."""
    executor = Executor(tracker=Breaking(TrackerRefused("400: project does not exist")), notes=NoteOnly())
    status, ref = executor(_action())
    assert status is ActionStatus.FAILED and ref is None


def test_an_unexpected_adapter_bug_does_not_lose_the_approval() -> None:
    executor = Executor(tracker=Breaking(ZeroDivisionError("oops")), notes=NoteOnly())
    status, _ref = executor(_action())
    assert status is ActionStatus.FAILED_RETRYABLE


def test_an_internal_update_needs_no_tracker() -> None:
    executor = Executor(tracker=Breaking(TrackerTimeout("would have failed")), notes=NoteOnly())
    status, ref = executor(_action(action_type="internal_update"))
    assert status is ActionStatus.EXECUTED and ref == "note:a1"


def test_every_attempt_is_recorded_including_the_failures() -> None:
    """A retry nobody logged is a mystery in the morning."""
    seen: list[tuple[str, str, str | None]] = []

    def recorder(tool: str, action: Action, outcome: str, error: str | None) -> None:
        seen.append((tool, outcome, error))

    Executor(tracker=Breaking(TrackerTimeout("gone")), notes=NoteOnly(), record_call=recorder)(_action())
    Executor(tracker=_Working(), notes=NoteOnly(), record_call=recorder)(_action("a2"))

    assert [s[1] for s in seen] == ["timeout", "ok"]
    assert seen[0][0] == "jira" and seen[0][2]


@dataclass
class _Working:
    def create(self, action: Action) -> str:
        return "SC-0001"


# --- the live adapter's wire behaviour ----------------------------------------------------------


def _live(handler) -> LiveJira:  # noqa: ANN001 - httpx handler
    return LiveJira(base_url="https://example.atlassian.net", email="a@b.c", token="t",
                    project_key="SC", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_the_live_adapter_returns_the_key_it_was_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue"
        assert b"PLT-02" in request.content
        return httpx.Response(201, json={"key": "SC-42"})

    assert _live(handler).create(_action()) == "SC-42"


def test_a_server_error_is_retryable_and_a_client_error_is_not() -> None:
    with pytest.raises(TrackerTimeout):
        _live(lambda r: httpx.Response(503, text="unavailable")).create(_action())
    with pytest.raises(TrackerRefused, match="400"):
        _live(lambda r: httpx.Response(400, text="bad project")).create(_action())


def test_a_network_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TrackerTimeout, match="did not answer|could not reach"):
        _live(handler).create(_action())


def test_an_accepted_issue_with_no_key_is_a_refusal_not_a_success() -> None:
    with pytest.raises(TrackerRefused, match="no key"):
        _live(lambda r: httpx.Response(201, json={"id": "10001"})).create(_action())


# --- choosing the adapter ----------------------------------------------------------------------


@dataclass
class _Settings:
    jira_mode: str


@pytest.mark.postgres
def test_mock_is_the_default_and_writes_somewhere_a_demo_can_point_at(db: Engine) -> None:
    executor = build_executor(_Settings(jira_mode="mock"), db)
    status, key = executor(_action())

    assert status is ActionStatus.EXECUTED and key and key.startswith("SC-")
    with db.connect() as conn:
        row = conn.execute(text("SELECT summary, assignee FROM ops.mock_jira WHERE key = :k"),
                           {"k": key}).mappings().one()
        calls = conn.execute(text("SELECT tool, outcome FROM ops.tool_calls")).mappings().all()
    assert "OTIF" in row["summary"] and row["assignee"] == "plant operations lead"
    assert [(c["tool"], c["outcome"]) for c in calls] == [("jira", "ok")]


@pytest.mark.postgres
def test_mock_keys_do_not_collide(db: Engine) -> None:
    executor = build_executor(_Settings(jira_mode="mock"), db)
    first = executor(_action("a1"))[1]
    second = executor(_action("a2"))[1]
    assert first != second


@pytest.mark.postgres
def test_live_mode_without_credentials_refuses_to_start(db: Engine, monkeypatch) -> None:
    """A live adapter that quietly falls back to the mock looks real and does nothing. Worst of both."""
    for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="JIRA_MODE=live needs"):
        build_executor(_Settings(jira_mode="live"), db)


@pytest.mark.postgres
def test_an_unknown_jira_mode_is_refused(db: Engine) -> None:
    with pytest.raises(ValueError, match="must be 'mock' or 'live'"):
        build_executor(_Settings(jira_mode="maybe"), db)


# --- the record ---------------------------------------------------------------------------------


@pytest.mark.postgres
def test_a_run_is_started_once_per_day_per_reader(db: Engine) -> None:
    first = store.start_run(db, LANE_DAY, "vp")
    again = store.start_run(db, LANE_DAY, "vp")
    other = store.start_run(db, LANE_DAY, "plt01")
    assert first == again and other != first


@pytest.mark.postgres
def test_the_brief_and_its_pack_are_kept_whole(db: Engine) -> None:
    """A number in a ticket is only checkable against the evidence that produced it."""
    run_id = store.start_run(db, LANE_DAY, "vp")
    store.save_brief(db, run_id, "t1", LANE_DAY, "vp", {"items": [{"id": "I1"}]},
                     {"summary": "one item"}, "llm")
    store.save_brief(db, run_id, "t1", LANE_DAY, "vp", {"items": [{"id": "I1"}, {"id": "I2"}]},
                     {"summary": "two items"}, "fallback")   # a re-run updates in place

    saved = store.brief_for(db, "t1")
    assert saved is not None
    assert saved["brief"]["summary"] == "two items" and saved["narrated_by"] == "fallback"
    assert len(saved["pack"]["items"]) == 2


@pytest.mark.postgres
def test_blocked_actions_are_recorded_too(db: Engine) -> None:
    """A refusal nobody can see is a policy drifting in the dark."""
    run_id = store.start_run(db, LANE_DAY, "vp")
    blocked = _action("blocked-1").with_status(ActionStatus.BLOCKED, blocked_reason="not allowed here")
    store.save_actions(db, run_id, "t1", LANE_DAY, [_action("ok-1"), blocked],
                       {"I1": ("PLT-02 C000031", "otif_rate")})

    with db.connect() as conn:
        rows = conn.execute(text("SELECT action_id, status, blocked_reason FROM ops.actions "
                                 "ORDER BY action_id")).mappings().all()
    assert [r["action_id"] for r in rows] == ["blocked-1", "ok-1"]
    assert rows[0]["blocked_reason"] == "not allowed here"


@pytest.mark.postgres
def test_suppression_reads_what_is_actually_open(db: Engine) -> None:
    """The ticket the gate needs to know about was raised days ago by another process."""
    run_id = store.start_run(db, LANE_DAY, "vp")
    executed = _action("done-1").with_status(ActionStatus.EXECUTED, external_ref="SC-0007")
    rejected = _action("no-1").with_status(ActionStatus.REJECTED)
    store.save_actions(db, run_id, "t1", LANE_DAY, [executed, rejected],
                       {"I1": ("PLT-02 C000031", "otif_rate")})

    still_open = store.open_actions(db)
    assert [a.external_ref for a in still_open] == ["SC-0007"]
    assert still_open[0].segment_label == "PLT-02 C000031" and still_open[0].metric == "otif_rate"
    assert all(a.status is not ActionStatus.REJECTED for a in still_open)


@pytest.mark.postgres
def test_a_retryable_failure_is_listed_for_the_next_run(db: Engine) -> None:
    run_id = store.start_run(db, LANE_DAY, "vp")
    failed = _action("retry-1").with_status(ActionStatus.FAILED_RETRYABLE)
    store.save_actions(db, run_id, "t1", LANE_DAY, [failed], {"I1": ("PLT-02", "otif_rate")})

    assert store.retryable_actions(db) == ["retry-1"]
    store.mark_executed(db, "retry-1", ActionStatus.EXECUTED, "SC-0009")
    assert store.retryable_actions(db) == []


@pytest.mark.postgres
def test_a_decision_survives_the_action_being_updated(db: Engine) -> None:
    """"Who approved this, and what did they change?" must not be overwritten by what happened next."""
    run_id = store.start_run(db, LANE_DAY, "vp")
    action = _action("a1")
    store.save_actions(db, run_id, "t1", LANE_DAY, [action], {"I1": ("PLT-02", "otif_rate")})
    store.save_decisions(db, [Decision(action_id="a1", verdict="edit", decided_by="plt02",
                                       title="Chase the carrier", body="Call them.", note="narrowed")])
    store.save_actions(db, run_id, "t1", LANE_DAY,
                       [action.with_status(ActionStatus.EXECUTED, external_ref="SC-0011",
                                           decided_by="plt02")],
                       {"I1": ("PLT-02", "otif_rate")})

    with db.connect() as conn:
        decision = conn.execute(text("SELECT verdict, decided_by, title, note FROM ops.decisions"))\
            .mappings().one()
        current = conn.execute(text("SELECT status, external_ref FROM ops.actions")).mappings().one()
    assert decision["verdict"] == "edit" and decision["decided_by"] == "plt02"
    assert decision["title"] == "Chase the carrier" and decision["note"] == "narrowed"
    assert current["status"] == "EXECUTED" and current["external_ref"] == "SC-0011"


@pytest.mark.postgres
def test_an_old_open_action_can_be_excluded_by_date(db: Engine) -> None:
    run_id = store.start_run(db, LANE_DAY, "vp")
    store.save_actions(db, run_id, "t1", LANE_DAY, [_action("a1")], {"I1": ("PLT-02", "otif_rate")})
    assert store.open_actions(db, before=LANE_DAY - timedelta(days=1)) == []
    assert len(store.open_actions(db, before=LANE_DAY)) == 1


@pytest.mark.postgres
def test_recent_runs_reads_back_what_the_ops_page_will_show(db: Engine) -> None:
    run_id = store.start_run(db, LANE_DAY, "vp")
    store.save_brief(db, run_id, "t1", LANE_DAY, "vp", {}, {"summary": "s"}, "llm")
    store.finish_run(db, run_id, "EXECUTED", items=3, narrated_by="llm")

    rows = store.recent_runs(db)
    assert rows and rows[0]["status"] == "EXECUTED" and rows[0]["items"] == 3
    assert rows[0]["thread_id"] == "t1" and rows[0]["finished_at"] is not None
