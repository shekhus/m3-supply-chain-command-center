"""Where an approved action actually goes.

Three types, three destinations, one rule: **a failure here is never an exception in somebody's face.** The
person approved something at 14:00 and walked away; the tracker being down is the system's problem, not
theirs. Every outcome is a status the next run can act on:

- a timeout or a 5xx → `FAILED_RETRYABLE`. The action stays open, the console shows it, the next run tries
  again, and `ops/summary` raises it as an alert (week 10).
- a 4xx → `FAILED`. The request was wrong, and retrying a wrong request forever is how a queue fills with
  identical failures. A person has to look.
- success → `EXECUTED` with the key, so the brief, the ticket and the audit row all point at each other.

`JIRA_MODE=mock` is the default and writes to `ops.mock_jira`, which is what the demo and every test use. The
live adapter is the same code path with a different `Tracker` behind it — that is the point of the seam: the
mock is not a stub that skips logic, it is a tracker that happens to live in Postgres.

Nothing here ever writes to M3 (principle 8). The destinations are a tracker and a note table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import Engine, text

from act.models import Action, ActionStatus

JIRA_TIMEOUT_S = 10.0
ISSUE_TYPES = {"jira_ticket": "Task", "investigation": "Task"}


class Tracker(Protocol):
    """Whatever creates the ticket. Mock and live differ only here."""

    def create(self, action: Action) -> str: ...


class TrackerTimeout(RuntimeError):
    """The tracker did not answer in time. Retryable: the request may or may not have landed."""


class TrackerRefused(RuntimeError):
    """The tracker answered and said no. Not retryable without a person changing something."""


@dataclass
class MockJira:
    """A tracker that lives in Postgres. The demo points at it, and so does every test."""

    engine: Engine
    prefix: str = "SC"

    def create(self, action: Action) -> str:
        with self.engine.begin() as conn:
            number = int(conn.execute(text("SELECT count(*) + 1 FROM ops.mock_jira")).scalar_one())
            key = f"{self.prefix}-{number:04d}"
            conn.execute(text(
                "INSERT INTO ops.mock_jira (key, action_id, summary, description, assignee) "
                "VALUES (:key, :action_id, :summary, :description, :assignee)"),
                {"key": key, "action_id": action.id, "summary": action.title,
                 "description": action.body, "assignee": action.assignee_hint})
        return key


@dataclass
class LiveJira:
    """Jira Cloud's REST API. Credentials come from the environment; nothing is ever hard-coded."""

    base_url: str
    email: str
    token: str
    project_key: str
    http: httpx.Client | None = None

    def create(self, action: Action) -> str:
        client = self.http or httpx.Client(timeout=JIRA_TIMEOUT_S)
        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": action.title,
                "description": {"type": "doc", "version": 1, "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": action.body}]}]},
                "issuetype": {"name": ISSUE_TYPES.get(action.type, "Task")},
            }
        }
        try:
            response = client.post(f"{self.base_url.rstrip('/')}/rest/api/3/issue", json=payload,
                                   auth=(self.email, self.token))
        except httpx.TimeoutException as exc:
            raise TrackerTimeout(f"jira did not answer within {JIRA_TIMEOUT_S:.0f}s: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TrackerTimeout(f"could not reach jira: {exc}") from exc

        if response.status_code >= 500:
            raise TrackerTimeout(f"jira returned {response.status_code}")
        if not response.is_success:
            raise TrackerRefused(f"jira returned {response.status_code}: {response.text[:300]}")
        try:
            return str(response.json()["key"])
        except (ValueError, KeyError) as exc:
            raise TrackerRefused(f"jira accepted the issue but returned no key: {exc}") from exc


@dataclass
class NoteOnly:
    """For `internal_update`: nothing to create anywhere, but it still has to be recorded and delivered."""

    engine: Engine | None = None

    def create(self, action: Action) -> str:
        return f"note:{action.id}"


class CallRecorder(Protocol):
    def __call__(self, tool: str, action: Action, outcome: str, error: str | None) -> None: ...


@dataclass
class Executor:
    """Dispatches an approved action to the right destination and turns failures into statuses."""

    tracker: Tracker
    notes: Tracker
    record_call: CallRecorder | None = None

    def __call__(self, action: Action) -> tuple[ActionStatus, str | None]:
        destination = self.notes if action.type == "internal_update" else self.tracker
        tool = "note" if action.type == "internal_update" else "jira"
        try:
            reference = destination.create(action)
        except TrackerTimeout as exc:
            self._record(tool, action, "timeout", str(exc))
            return ActionStatus.FAILED_RETRYABLE, None
        except TrackerRefused as exc:
            self._record(tool, action, "refused", str(exc))
            return ActionStatus.FAILED, None
        except Exception as exc:                     # an adapter bug must not lose the approval
            self._record(tool, action, "error", f"{type(exc).__name__}: {exc}")
            return ActionStatus.FAILED_RETRYABLE, None
        self._record(tool, action, "ok", None)
        return ActionStatus.EXECUTED, reference

    def _record(self, tool: str, action: Action, outcome: str, error: str | None) -> None:
        if self.record_call is not None:
            self.record_call(tool, action, outcome, error)


@dataclass
class DbCallRecorder:
    """Every attempt on `ops.tool_calls`, failures included: a retry nobody logged is a mystery."""

    engine: Engine
    run_id: int | None = None

    def __call__(self, tool: str, action: Action, outcome: str, error: str | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO ops.tool_calls (run_id, tool, arguments, outcome, error) "
                "VALUES (:run_id, :tool, CAST(:arguments AS jsonb), :outcome, :error)"),
                {"run_id": self.run_id, "tool": tool,
                 "arguments": json.dumps({"action_id": action.id, "type": action.type,
                                          "title": action.title}),
                 "outcome": outcome, "error": (error or "")[:2000] or None})


def build_executor(settings: object, engine: Engine, run_id: int | None = None) -> Executor:
    """Mock unless `JIRA_MODE=live`, and live refuses to start without its credentials.

    A live adapter that silently falls back to the mock when a variable is missing is the worst of both: the
    demo looks real and nothing was ever created.
    """
    import os

    mode = str(getattr(settings, "jira_mode", "mock")).lower()
    recorder = DbCallRecorder(engine, run_id)
    if mode == "mock":
        return Executor(tracker=MockJira(engine), notes=NoteOnly(engine), record_call=recorder)
    if mode != "live":
        raise ValueError(f"JIRA_MODE must be 'mock' or 'live', not {mode!r}")

    missing = [name for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY")
               if not os.environ.get(name)]
    if missing:
        raise ValueError(f"JIRA_MODE=live needs {', '.join(missing)}")
    live = LiveJira(base_url=os.environ["JIRA_BASE_URL"], email=os.environ["JIRA_EMAIL"],
                    token=os.environ["JIRA_API_TOKEN"], project_key=os.environ["JIRA_PROJECT_KEY"])
    return Executor(tracker=live, notes=NoteOnly(engine), record_call=recorder)
