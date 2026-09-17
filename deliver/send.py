"""Sending the brief — and never letting the sending lose it.

The brief is written, validated and recorded before anything is delivered. Delivery is therefore the *last*
thing that happens and the least important: if the SMTP server is down, the brief still exists, the console
still shows it, and the record still says what was proposed. So every failure here is caught, recorded on
`ops.tool_calls`, and returned as a result — never raised into the run that produced the brief.

Recipients are configured per audience. A brief filtered for one plant and then emailed to everybody would
undo the permission filter at the last possible step, in the medium most likely to be forwarded.
"""

from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx
from sqlalchemy import Engine, text

from deliver.render import as_slack, as_text, subject
from graph import RunView

SLACK_TIMEOUT_S = 10.0
SMTP_TIMEOUT_S = 20.0


@dataclass
class DeliveryResult:
    """What was attempted and what happened. Reported, never thrown."""

    channel: str
    outcome: str                      # sent | skipped | failed
    detail: str | None = None
    recipients: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome in ("sent", "skipped")


def recipients_for(audience: str) -> list[str]:
    """`DELIVERY_RECIPIENTS=vp:a@x.com;b@x.com,plt01:c@x.com` — per audience, never a single global list."""
    raw = os.environ.get("DELIVERY_RECIPIENTS", "")
    for group in raw.split(","):
        name, _, people = group.partition(":")
        if name.strip() == audience and people.strip():
            return [person.strip() for person in people.split(";") if person.strip()]
    return []


def deliver(view: RunView, console_url: str, engine: Engine | None = None,
            mode: str | None = None) -> list[DeliveryResult]:
    """Send the brief by whatever `DELIVERY_MODE` names. Failures are recorded and returned."""
    chosen = (mode or os.environ.get("DELIVERY_MODE", "none")).lower()
    channels = [c.strip() for c in chosen.split("+") if c.strip()]
    results: list[DeliveryResult] = []

    for channel in channels:
        if channel == "none":
            results.append(DeliveryResult("none", "skipped", "DELIVERY_MODE=none"))
        elif channel == "email":
            results.append(_send_email(view, console_url))
        elif channel == "slack":
            results.append(_send_slack(view, console_url))
        else:
            results.append(DeliveryResult(channel, "failed", f"unknown delivery channel {channel!r}"))
    for result in results:
        _record(engine, view, result)
    return results


def _send_email(view: RunView, console_url: str) -> DeliveryResult:
    people = recipients_for(view.audience)
    if not people:
        return DeliveryResult("email", "skipped", f"no recipients configured for '{view.audience}'")
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("EMAIL_FROM")
    if not host or not sender:
        return DeliveryResult("email", "failed", "SMTP_HOST and EMAIL_FROM are required for email delivery",
                              people)

    message = EmailMessage()
    message["Subject"] = subject(view)
    message["From"] = sender
    message["To"] = ", ".join(people)
    message.set_content(as_text(view, console_url))

    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")),
                          timeout=SMTP_TIMEOUT_S) as smtp:
            if os.environ.get("SMTP_STARTTLS", "1") == "1":
                smtp.starttls()
            user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
            if user and password:
                smtp.login(user, password)
            smtp.send_message(message)
    except Exception as exc:              # a mail server is somebody else's uptime, not this run's
        return DeliveryResult("email", "failed", f"{type(exc).__name__}: {exc}"[:400], people)
    return DeliveryResult("email", "sent", f"{len(people)} recipient(s)", people)


def _send_slack(view: RunView, console_url: str, http: httpx.Client | None = None) -> DeliveryResult:
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return DeliveryResult("slack", "skipped", "SLACK_WEBHOOK_URL is not set")
    client = http or httpx.Client(timeout=SLACK_TIMEOUT_S)
    try:
        response = client.post(webhook, json=as_slack(view, console_url))
    except httpx.HTTPError as exc:
        return DeliveryResult("slack", "failed", f"could not reach slack: {exc}"[:400])
    if not response.is_success:
        return DeliveryResult("slack", "failed", f"slack returned {response.status_code}: "
                                                 f"{response.text[:200]}")
    return DeliveryResult("slack", "sent", "webhook accepted")


def _record(engine: Engine | None, view: RunView, result: DeliveryResult) -> None:
    if engine is None:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO ops.tool_calls (tool, arguments, outcome, error) "
                "VALUES (:tool, CAST(:arguments AS jsonb), :outcome, :error)"),
                {"tool": f"deliver:{result.channel}",
                 "arguments": json.dumps({"thread_id": view.thread_id, "audience": view.audience,
                                          "recipients": result.recipients}),
                 "outcome": result.outcome,
                 "error": result.detail if result.outcome == "failed" else None})
    except Exception:                     # recording a delivery must not break delivery
        return
