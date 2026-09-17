"""What an operator needs at 08:00, and what the RUNBOOK's alerts are named after.

`/ops/summary` answers one question — *is this working?* — with the four things that can be wrong and are not
visible from a brief: nothing ran, the model stopped being usable, approvals are piling up, or actions are
failing at the tracker. Each alert here has a section in `docs/RUNBOOK.md` with the same name, so paging
somebody at 07:00 hands them a page rather than a number.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import Engine, text

from app.auth import require
from app.db import get_engine

router = APIRouter(prefix="/ops", tags=["ops"])

STALE_RUN_DAYS = 2            # no brief for this long is the loudest thing this system can say
PENDING_DAYS = 3              # an approval waiting longer than this is a brief nobody read
FALLBACK_PCT_ALERT = 50.0     # more than half the briefs templated means the model is not usable


def _rows(engine: Engine, sql: str, **params: Any) -> list[dict]:
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


@router.get("/summary", dependencies=[Depends(require("viewer", "analyst", "owner"))])
def summary(engine: Annotated[Engine, Depends(get_engine)], days: int = 14) -> dict:
    """The health of the last fortnight, plus any alert an operator should act on now."""
    since = date.today() - timedelta(days=days)
    runs = _rows(engine, "SELECT * FROM ops.v_run_health WHERE run_date >= :since "
                         "ORDER BY run_date DESC, audience", since=since)
    narration = _rows(engine, "SELECT * FROM ops.v_narration_health WHERE run_date >= :since "
                              "ORDER BY run_date DESC", since=since)
    usage = _rows(engine, "SELECT * FROM ops.v_model_usage WHERE day >= :since ORDER BY day DESC, purpose",
                  since=since)
    pending = _rows(engine, "SELECT * FROM ops.v_pending_approvals ORDER BY days_waiting DESC, run_date")
    failures = _rows(engine, "SELECT * FROM ops.v_action_failures ORDER BY run_date DESC")
    refusals = _rows(engine, "SELECT * FROM ops.v_policy_refusals WHERE run_date >= :since "
                             "ORDER BY run_date DESC", since=since)
    delivery = _rows(engine, "SELECT * FROM ops.v_delivery WHERE day >= :since ORDER BY day DESC",
                     since=since)

    return {
        "window_days": days,
        "alerts": _alerts(runs, narration, pending, failures),
        "runs": runs,
        "narration": narration,
        "model_usage": usage,
        "pending_approvals": pending,
        "action_failures": failures,
        "policy_refusals": refusals,
        "delivery": delivery,
        "cost_usd_window": round(sum(float(r["cost_usd"] or 0) for r in runs), 4),
    }


def _alerts(runs: list[dict], narration: list[dict], pending: list[dict],
            failures: list[dict]) -> list[dict]:
    """Named to match `docs/RUNBOOK.md`, because an alert without a page is a pager without an answer."""
    alerts: list[dict] = []

    latest = max((r["run_date"] for r in runs), default=None)
    if latest is None:
        alerts.append({"name": "no-briefs", "severity": "HIGH",
                       "detail": "no brief has been produced in this window"})
    elif (date.today() - latest).days > STALE_RUN_DAYS:
        alerts.append({"name": "no-briefs", "severity": "HIGH",
                       "detail": f"the last brief was {latest.isoformat()}, "
                                 f"{(date.today() - latest).days} days ago"})

    recent = narration[:7]
    briefs = sum(r["briefs"] for r in recent)
    fell_back = sum(r["fell_back"] for r in recent)
    if briefs and 100.0 * fell_back / briefs > FALLBACK_PCT_ALERT:
        alerts.append({"name": "narration-falling-back", "severity": "WARN",
                       "detail": f"{fell_back} of the last {briefs} briefs were templated; "
                                 "the model is failing or the validator is rejecting it"})

    stuck = [p for p in pending if p["days_waiting"] > PENDING_DAYS]
    if stuck:
        alerts.append({"name": "approvals-piling-up", "severity": "WARN",
                       "detail": f"{len(stuck)} action(s) have waited more than {PENDING_DAYS} days; "
                                 f"oldest is {max(p['days_waiting'] for p in stuck)} days"})

    retryable = [f for f in failures if f["status"] == "FAILED_RETRYABLE"]
    hard = [f for f in failures if f["status"] == "FAILED"]
    if hard:
        alerts.append({"name": "action-failed", "severity": "HIGH",
                       "detail": f"{len(hard)} action(s) were refused by the tracker and need a person"})
    if retryable:
        alerts.append({"name": "action-retrying", "severity": "WARN",
                       "detail": f"{len(retryable)} action(s) failed and will be retried"})
    return alerts


@router.get("/runs", dependencies=[Depends(require("viewer", "analyst", "owner"))])
def runs(engine: Annotated[Engine, Depends(get_engine)], limit: int = 50) -> list[dict]:
    return _rows(engine, "SELECT * FROM ops.v_run_health ORDER BY run_date DESC, audience LIMIT :limit",
                 limit=limit)
