"""Delivery: copy the brief, address it to the right people, and never lose it when sending fails.

Three promises are tested. The rendering invents nothing — every sentence and every number already exists in
the validated brief. The recipients are per audience, so a brief filtered for one plant is not emailed to
everybody at the last possible step. And a failure to send is a recorded result, never an exception into the
run that produced the brief: a missing brief is a silent failure, a missing email is a noisy inconvenience.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

import httpx
import pytest

from act.models import Action
from deliver.render import as_slack, as_text, subject
from deliver.send import DeliveryResult, deliver, recipients_for
from graph import RunView
from narrate.contract import Brief, BriefItem, Claim

CONSOLE = "https://console.example.com"


def _view(items: int = 2, pending: int = 1, freshness: str | None = "Data is current to 2025-09-15.",
          severity: Literal["WARN", "HIGH"] = "HIGH") -> RunView:
    brief = Brief(
        summary="OTIF fell at PLT-02 and the backlog rose.",
        freshness_line=freshness,
        items=[BriefItem(id=f"I{n}", headline=f"OTIF down to 50.0% at PLT-0{n}",
                         why=f"OTIF fell to 50.0% against a normal of 87.5%. Item {n}.",
                         severity=severity if n == 1 else "WARN",
                         claims=[Claim(text="OTIF was 50.0%.", metric_ref=f"I{n}.value")])
               for n in range(1, items + 1)])
    actions = [Action(id=f"a{n}", item_id=f"I{n}", anomaly_type="otif_drop", type="jira_ticket",
                      title=f"[HIGH] OTIF down to 50.0% at PLT-0{n}", body="body",
                      assignee_hint="plant operations lead")
               for n in range(1, pending + 1)]
    return RunView(thread_id="2025-09-15:vp", run_date=date(2025, 9, 15), audience="vp",
                   status="PENDING_APPROVAL", next_nodes=("execute",), brief=brief, pack=None,
                   actions=actions, gate=None, narration={"source": "llm"}, executed=[])


# --- rendering copies, it does not write --------------------------------------------------------


def test_the_text_says_only_what_the_brief_says() -> None:
    view = _view()
    text = as_text(view, CONSOLE)

    assert view.brief is not None
    assert view.brief.summary in text
    for item in view.brief.items:
        assert item.headline in text and item.why in text
        for claim in item.claims:
            assert claim.text in text and claim.metric_ref in text


def test_the_message_links_to_the_console_rather_than_inviting_a_reply() -> None:
    """Approving happens where the evidence is. A mail that approves by reply approves unread."""
    text = as_text(_view(), CONSOLE)
    assert f"{CONSOLE}/?thread=2025-09-15:vp" in text
    assert "reply" not in text.lower()


def test_a_stale_brief_says_so_before_anything_else() -> None:
    text = as_text(_view(freshness="Stale data: delivery metrics are 3 days old."), CONSOLE)
    body = text.split("\n")
    assert any("Stale data" in line for line in body[:5])


def test_the_subject_says_how_much_there_is_to_do() -> None:
    assert "1 to look at today" in subject(_view(items=2, severity="HIGH"))
    assert "2 items" in subject(_view(items=2, severity="WARN"))

    quiet = _view(items=0, pending=0)
    assert quiet.brief is not None
    assert "nothing to review" in subject(quiet)


def test_a_quiet_morning_still_sends_something_readable() -> None:
    text = as_text(_view(items=0, pending=0), CONSOLE)
    assert "Nothing needs your approval today." in text


def test_slack_keeps_it_short_and_points_at_the_console() -> None:
    view = _view(items=8, pending=2)
    payload = as_slack(view, CONSOLE)
    sections = [b for b in payload["blocks"] if b["type"] == "section"]

    assert payload["text"] == subject(view)
    assert len(sections) <= 7                      # summary + five items + the approval line
    assert any("more in the console" in str(b) for b in payload["blocks"])
    assert any(CONSOLE in str(b) for b in payload["blocks"])


# --- recipients are per audience ----------------------------------------------------------------


def test_recipients_are_looked_up_by_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    """A brief filtered for one plant must not be emailed to everybody at the last step."""
    monkeypatch.setenv("DELIVERY_RECIPIENTS", "vp:vp@x.com;coo@x.com,plt01:plt01@x.com")
    assert recipients_for("vp") == ["vp@x.com", "coo@x.com"]
    assert recipients_for("plt01") == ["plt01@x.com"]
    assert recipients_for("plt02") == []           # nobody configured: nothing is sent


def test_email_without_recipients_is_skipped_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_RECIPIENTS", "plt01:someone@x.com")
    monkeypatch.setenv("DELIVERY_MODE", "email")
    results = deliver(_view(), CONSOLE)
    assert [r.outcome for r in results] == ["skipped"]
    assert "no recipients configured for 'vp'" in (results[0].detail or "")


# --- failures are results, not exceptions -------------------------------------------------------


def test_delivery_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELIVERY_MODE", raising=False)
    results = deliver(_view(), CONSOLE)
    assert [(r.channel, r.outcome) for r in results] == [("none", "skipped")]


def test_a_missing_smtp_configuration_fails_loudly_but_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_MODE", "email")
    monkeypatch.setenv("DELIVERY_RECIPIENTS", "vp:vp@x.com")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("EMAIL_FROM", raising=False)

    results = deliver(_view(), CONSOLE)             # must not raise
    assert results[0].outcome == "failed"
    assert "SMTP_HOST" in (results[0].detail or "")


def test_an_smtp_outage_does_not_reach_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The brief exists, the console has it, the record is written. The mail server is somebody else's day."""
    monkeypatch.setenv("DELIVERY_MODE", "email")
    monkeypatch.setenv("DELIVERY_RECIPIENTS", "vp:vp@x.com")
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("SMTP_PORT", "1")            # nothing listens there
    monkeypatch.setenv("EMAIL_FROM", "brief@x.com")

    results = deliver(_view(), CONSOLE)
    assert results[0].outcome == "failed" and results[0].recipients == ["vp@x.com"]


def test_slack_without_a_webhook_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_MODE", "slack")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert deliver(_view(), CONSOLE)[0].outcome == "skipped"


def test_a_slack_rejection_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_MODE", "slack")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")

    import deliver.send as send_module

    def failing(view, console_url, http=None):  # noqa: ANN001, ANN202 - a stand-in
        return DeliveryResult("slack", "failed", "slack returned 404: no_service")

    monkeypatch.setattr(send_module, "_send_slack", failing)
    results = deliver(_view(), CONSOLE)
    assert results[0].outcome == "failed" and "404" in (results[0].detail or "")


def test_an_unknown_channel_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_MODE", "carrier-pigeon")
    results = deliver(_view(), CONSOLE)
    assert results[0].outcome == "failed" and "unknown delivery channel" in (results[0].detail or "")


def test_two_channels_are_both_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_MODE", "email+slack")
    monkeypatch.delenv("DELIVERY_RECIPIENTS", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert [r.channel for r in deliver(_view(), CONSOLE)] == ["email", "slack"]


def test_slack_posts_the_blocks_it_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.read()))
        return httpx.Response(200, text="ok")

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")
    import deliver.send as send_module

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = send_module._send_slack(_view(), CONSOLE, http=client)
    assert result.outcome == "sent"
    assert posted["text"] == subject(_view())
    assert posted["blocks"][0]["type"] == "header"
