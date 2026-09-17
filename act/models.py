"""What the system may propose, and what happens to it afterwards.

An action is a *proposal*. Nothing here executes on creation, nothing reaches a system of record without a
person pressing approve, and nothing here ever writes to M3 — the ERP is read-only to this system, for good
(CLAUDE.md principle 8). The three types are deliberately modest:

- `jira_ticket` — work for somebody, in the tracker they already use
- `investigation` — a question for a human, with the evidence attached
- `internal_update` — a note to a team, where nothing needs doing yet

Status is a small, explicit machine. `PROPOSED` is the only state the system reaches on its own; every other
transition is a person's decision or the result of executing one.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ActionType = Literal["jira_ticket", "investigation", "internal_update"]


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"                 # drafted and passed the gate; waiting for a person
    REJECTED = "REJECTED"                 # a person said no
    APPROVED = "APPROVED"                 # a person said yes; not yet executed
    EDITED = "EDITED"                     # a person changed the wording, then said yes
    EXECUTED = "EXECUTED"                 # the adapter did it, and returned a reference
    FAILED_RETRYABLE = "FAILED_RETRYABLE"  # the adapter timed out; the next run tries again
    FAILED = "FAILED"                     # the adapter refused; a person has to look
    SUPPRESSED = "SUPPRESSED"             # a duplicate of something already open
    BLOCKED = "BLOCKED"                   # policy refused it before anyone was asked


class Action(BaseModel):
    """One thing a person may be asked to approve."""

    model_config = ConfigDict(frozen=True)

    id: str
    item_id: str                          # the pack item this answers
    anomaly_type: str                     # the policy key that decides what is allowed here
    type: ActionType
    title: str = Field(max_length=120)
    body: str
    assignee_hint: str
    evidence_refs: list[str] = Field(default_factory=list)
    status: ActionStatus = ActionStatus.PROPOSED
    blocked_reason: str | None = None
    external_ref: str | None = None       # the ticket key, once it exists
    decided_by: str | None = None
    decided_at: datetime | None = None

    def with_status(self, status: ActionStatus, **over: object) -> Action:
        return self.model_copy(update={"status": status, **over})

    @property
    def is_open(self) -> bool:
        return self.status in {ActionStatus.PROPOSED, ActionStatus.APPROVED, ActionStatus.EDITED,
                               ActionStatus.FAILED_RETRYABLE}


class OpenAction(BaseModel):
    """An action already out in the world, for duplicate suppression."""

    model_config = ConfigDict(frozen=True)

    anomaly_type: str
    segment_label: str
    metric: str
    type: ActionType
    created_on: date
    external_ref: str | None = None
    status: ActionStatus = ActionStatus.EXECUTED


class Decision(BaseModel):
    """A person's answer. Recorded whole: who, when, and what they changed."""

    model_config = ConfigDict(frozen=True)

    action_id: str
    verdict: Literal["approve", "edit", "reject"]
    decided_by: str
    note: str | None = None
    title: str | None = None              # set when the verdict is "edit"
    body: str | None = None
