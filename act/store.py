"""The durable record: what went out, what it proposed, who decided, and what it became.

Separate from the graph's checkpoint tables on purpose. The checkpointer owns a paused machine and LangGraph
owns its shape; this owns the business's answer to "what did we send on the 15th and who approved it?", which
has to survive a library upgrade, a graph redesign and a checkpoint being cleaned up.

It is also where duplicate suppression gets its facts. A gate that suppresses against an in-memory list
suppresses nothing in production — the ticket it needs to know about was raised four days ago, by a different
process, and only the database remembers it.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import Engine, text

from act.models import Action, ActionStatus, Decision, OpenAction

OPEN_STATUSES = (ActionStatus.PROPOSED.value, ActionStatus.APPROVED.value, ActionStatus.EDITED.value,
                 ActionStatus.EXECUTED.value, ActionStatus.FAILED_RETRYABLE.value)


def start_run(engine: Engine, run_date: date, audience: str) -> int:
    """One run row per day per reader. A second start returns the first, so a double cron is harmless."""
    with engine.begin() as conn:
        existing = conn.execute(text(
            "SELECT run_id FROM ops.runs WHERE run_date = :d AND audience = :a"),
            {"d": run_date, "a": audience}).scalar()
        if existing is not None:
            return int(existing)
        return int(conn.execute(text(
            "INSERT INTO ops.runs (run_date, audience) VALUES (:d, :a) RETURNING run_id"),
            {"d": run_date, "a": audience}).scalar_one())


def finish_run(engine: Engine, run_id: int, status: str, items: int, narrated_by: str,
               fallback_reason: str | None = None, error: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ops.runs SET finished_at = now(), status = :status, items = :items, "
            "narrated_by = :narrated_by, fallback_reason = :fallback_reason, error = :error "
            "WHERE run_id = :run_id"),
            {"run_id": run_id, "status": status, "items": items, "narrated_by": narrated_by,
             "fallback_reason": (fallback_reason or "")[:2000] or None,
             "error": (error or "")[:2000] or None})


def save_brief(engine: Engine, run_id: int, thread_id: str, run_date: date, audience: str,
               pack: dict, brief: dict, narrated_by: str) -> None:
    """The pack and the brief as they were on the day.

    Kept whole because a number in a ticket is only checkable against the evidence that produced it, and that
    evidence is recomputed differently the moment a threshold moves.
    """
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO ops.briefs (run_id, thread_id, run_date, audience, pack, brief, narrated_by) "
            "VALUES (:run_id, :thread_id, :run_date, :audience, CAST(:pack AS jsonb), "
            "CAST(:brief AS jsonb), :narrated_by) "
            "ON CONFLICT (thread_id) DO UPDATE SET pack = EXCLUDED.pack, brief = EXCLUDED.brief, "
            "narrated_by = EXCLUDED.narrated_by"),
            {"run_id": run_id, "thread_id": thread_id, "run_date": run_date, "audience": audience,
             "pack": json.dumps(pack), "brief": json.dumps(brief), "narrated_by": narrated_by})


def save_actions(engine: Engine, run_id: int, thread_id: str, run_date: date, actions: list[Action],
                 segments: dict[str, tuple[str, str]]) -> None:
    """Every action, blocked and suppressed ones included: a refusal nobody sees is policy in the dark.

    `segments` maps an item id to its (segment_label, metric), so the suppression query can ask "is there an
    open action about this lane?" without re-deriving it from the pack.
    """
    if not actions:
        return
    rows = []
    for action in actions:
        segment_label, metric = segments.get(action.item_id, ("", ""))
        rows.append({
            "action_id": action.id, "run_id": run_id, "thread_id": thread_id, "run_date": run_date,
            "item_id": action.item_id, "anomaly_type": action.anomaly_type,
            "segment_label": segment_label, "metric": metric, "type": action.type,
            "title": action.title, "body": action.body, "assignee_hint": action.assignee_hint,
            "evidence_refs": json.dumps(action.evidence_refs), "status": action.status.value,
            "blocked_reason": action.blocked_reason, "external_ref": action.external_ref,
            "decided_by": action.decided_by, "decided_at": action.decided_at,
        })
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO ops.actions (action_id, run_id, thread_id, run_date, item_id, anomaly_type, "
            "segment_label, metric, type, title, body, assignee_hint, evidence_refs, status, "
            "blocked_reason, external_ref, decided_by, decided_at) "
            "VALUES (:action_id, :run_id, :thread_id, :run_date, :item_id, :anomaly_type, :segment_label, "
            ":metric, :type, :title, :body, :assignee_hint, CAST(:evidence_refs AS jsonb), :status, "
            ":blocked_reason, :external_ref, :decided_by, :decided_at) "
            "ON CONFLICT (action_id) DO UPDATE SET status = EXCLUDED.status, title = EXCLUDED.title, "
            "body = EXCLUDED.body, blocked_reason = EXCLUDED.blocked_reason, "
            "external_ref = EXCLUDED.external_ref, decided_by = EXCLUDED.decided_by, "
            "decided_at = EXCLUDED.decided_at"), rows)


def save_decisions(engine: Engine, decisions: list[Decision]) -> None:
    """Kept separately from the action's current state: "who approved this, and what did they change?"

    An action row is updated as it moves; a decision row never is. The audit answer has to survive the update.
    """
    if not decisions:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO ops.decisions (action_id, verdict, decided_by, note, title, body) "
            "VALUES (:action_id, :verdict, :decided_by, :note, :title, :body)"),
            [d.model_dump(mode="json") for d in decisions])


def open_actions(engine: Engine, before: date | None = None) -> list[OpenAction]:
    """What is still open, for the gate to suppress against. The real reason suppression works at all."""
    sql = ("SELECT anomaly_type, segment_label, metric, type, status, external_ref, "
           "       created_at::date AS created_on "
           "FROM ops.actions WHERE status = ANY(:statuses)")
    params: dict[str, object] = {"statuses": list(OPEN_STATUSES)}
    if before is not None:
        sql += " AND run_date <= :before"
        params["before"] = before
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [OpenAction(anomaly_type=r["anomaly_type"], segment_label=r["segment_label"],
                       metric=r["metric"], type=r["type"], created_on=r["created_on"],
                       external_ref=r["external_ref"], status=ActionStatus(r["status"]))
            for r in rows]


def retryable_actions(engine: Engine) -> list[str]:
    """Actions whose tracker call failed in a way the next run should try again (plan's Jira-timeout case)."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT action_id FROM ops.actions WHERE status = :status ORDER BY created_at"),
            {"status": ActionStatus.FAILED_RETRYABLE.value}).scalars().all()
    return [str(row) for row in rows]


def mark_executed(engine: Engine, action_id: str, status: ActionStatus, external_ref: str | None,
                  decided_by: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ops.actions SET status = :status, external_ref = :external_ref, "
            "decided_by = COALESCE(:decided_by, decided_by), decided_at = :now WHERE action_id = :id"),
            {"id": action_id, "status": status.value, "external_ref": external_ref,
             "decided_by": decided_by, "now": datetime.now(tz=None)})


def brief_for(engine: Engine, thread_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT pack, brief, narrated_by, run_date, audience FROM ops.briefs WHERE thread_id = :t"),
            {"t": thread_id}).mappings().first()
    return dict(row) if row else None


def recent_runs(engine: Engine, limit: int = 30) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT r.run_id, r.run_date, r.audience, r.status, r.items, r.narrated_by, r.fallback_reason, "
            "       r.started_at, r.finished_at, b.thread_id "
            "FROM ops.runs r LEFT JOIN ops.briefs b ON b.run_id = r.run_id "
            "ORDER BY r.run_date DESC, r.started_at DESC LIMIT :limit"), {"limit": limit}).mappings().all()
    return [dict(row) for row in rows]
