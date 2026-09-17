"""The brief: build one, read one, decide on it.

Three things this router is careful about:

1. **The audience comes from the key, never from the request.** A caller cannot ask for somebody else's brief
   by changing a parameter — that would make the permission filter decorative.
2. **Building is not deciding.** `POST /brief/run` is what the cron calls; it returns a brief paused before
   anything leaves the building. Only `POST /brief/{thread_id}/decide` can resume it, and only for an
   `owner`.
3. **Reading never advances anything.** `GET` loads the paused run from the checkpointer and returns it.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Engine

import service
from act.models import Decision
from act.policy_gate import PolicyError
from app.auth import Principal, current_principal, require
from app.db import get_engine
from graph import RunView, UnknownThread, thread_id_for

router = APIRouter(prefix="/brief", tags=["brief"])


class DecisionRequest(BaseModel):
    action_id: str
    verdict: Literal["approve", "edit", "reject"]
    note: str | None = None
    title: str | None = None
    body: str | None = None


class DecideRequest(BaseModel):
    decisions: list[DecisionRequest] = Field(min_length=1)


class ActionOut(BaseModel):
    id: str
    item_id: str
    type: str
    title: str
    body: str
    assignee_hint: str
    status: str
    blocked_reason: str | None = None
    external_ref: str | None = None
    decided_by: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class BriefOut(BaseModel):
    thread_id: str
    run_date: date
    audience: str
    status: str
    awaiting_approval: bool
    summary: str
    freshness_line: str | None
    narrated_by: str | None
    items: list[dict]
    actions: list[ActionOut]
    gate: dict | None
    executed: list[dict]


def _out(view: RunView) -> BriefOut:
    brief = view.brief
    narration = view.narration or {}
    return BriefOut(
        thread_id=view.thread_id, run_date=view.run_date, audience=view.audience, status=view.status,
        awaiting_approval=view.awaiting_approval,
        summary=brief.summary if brief else "", freshness_line=brief.freshness_line if brief else None,
        narrated_by=narration.get("source"),
        items=[item.model_dump(mode="json") for item in (brief.items if brief else [])],
        actions=[ActionOut(id=a.id, item_id=a.item_id, type=a.type, title=a.title, body=a.body,
                           assignee_hint=a.assignee_hint, status=a.status.value,
                           blocked_reason=a.blocked_reason, external_ref=a.external_ref,
                           decided_by=a.decided_by, evidence_refs=a.evidence_refs)
                 for a in view.actions],
        gate=view.gate, executed=view.executed)


@router.post("/run", response_model=BriefOut, dependencies=[Depends(require("analyst", "owner"))])
def run_brief(
    principal: Annotated[Principal, Depends(current_principal)],
    engine: Annotated[Engine, Depends(get_engine)],
    run_date: date | None = None,
) -> BriefOut:
    """Build the brief for a date and stop at the approval pause. What the morning cron calls."""
    when = run_date or date.today()
    try:
        view = service.run_brief(engine, when, principal.audience)
    except Exception as exc:  # the cron must get a status, not a stack trace
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"could not build the brief for {when}: {exc}") from exc
    return _out(view)


@router.get("/{thread_id}", response_model=BriefOut,
            dependencies=[Depends(require("viewer", "analyst", "owner"))])
def read_brief(
    thread_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> BriefOut:
    """Read a paused run. Looking at a brief never advances it."""
    _require_own_thread(thread_id, principal)
    try:
        view = service.view_run(engine, thread_id)
    except UnknownThread as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no brief for thread {thread_id}") from exc
    return _out(view)


@router.post("/{thread_id}/decide", response_model=BriefOut,
             dependencies=[Depends(require("owner"))])
def decide(
    thread_id: str,
    body: DecideRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> BriefOut:
    """Approve, edit or reject. The only way past the interrupt, and only for an owner."""
    _require_own_thread(thread_id, principal)
    decisions = [Decision(action_id=d.action_id, verdict=d.verdict, decided_by=principal.audience,
                          note=d.note, title=d.title, body=d.body) for d in body.decisions]
    try:
        view = service.apply_decisions(engine, thread_id, decisions)
    except PolicyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return _out(view)


def _require_own_thread(thread_id: str, principal: Principal) -> None:
    """A thread is one day's brief for one reader; a key may only touch its own.

    Without this, a plant manager could read the leadership brief simply by knowing the date — the pack
    filtering would have been for nothing.
    """
    _date, _, audience = thread_id.partition(":")
    if audience != principal.audience:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"this key may only see briefs for '{principal.audience}'")


def thread_for(run_date: date, principal: Principal) -> str:
    return thread_id_for(run_date, principal.audience)
