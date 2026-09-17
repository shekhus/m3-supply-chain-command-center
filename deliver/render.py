"""The brief, as an email and as a Slack message.

Rendering is copying. Every sentence here already exists in the validated brief, and the only things this
module adds are structure and a link to the console — because the moment a renderer starts summarising, it
becomes a second writer whose output nothing checked.

The link matters more than it looks: the message is a notification, not the system of record. Approving
happens in the console, where the evidence is, and a mail that invited approval by reply would be a mail that
approves without anybody seeing what they approved.
"""

from __future__ import annotations

from graph import RunView

MAX_SLACK_ITEMS = 5


def subject(view: RunView) -> str:
    brief = view.brief
    high = sum(1 for item in (brief.items if brief else []) if item.severity == "HIGH")
    date_part = view.run_date.isoformat()
    if not brief or not brief.items:
        return f"Morning brief {date_part}: nothing to review"
    if high:
        return f"Morning brief {date_part}: {high} to look at today"
    return f"Morning brief {date_part}: {len(brief.items)} item{'s' if len(brief.items) != 1 else ''}"


def as_text(view: RunView, console_url: str) -> str:
    """Plain text, because a brief that only renders in HTML is a brief somebody cannot read on a phone."""
    brief = view.brief
    if brief is None:
        return f"No brief was produced for {view.run_date.isoformat()}."

    lines = [subject(view), "=" * len(subject(view)), ""]
    if brief.freshness_line:
        lines += [brief.freshness_line, ""]
    lines += [brief.summary, ""]

    for item in brief.items:
        lines.append(f"[{item.severity}] {item.headline}")
        lines.append(f"  {item.why}")
        for claim in item.claims:
            lines.append(f"    - {claim.text}  ({claim.metric_ref})")
        lines.append("")

    pending = view.pending
    if pending:
        lines.append(f"Waiting for you ({len(pending)}):")
        lines += [f"  - {action.type.replace('_', ' ')}: {action.title}" for action in pending]
        lines.append("")
        lines.append(f"Approve, edit or reject in the console: {_link(console_url, view)}")
    else:
        lines.append("Nothing needs your approval today.")
    lines += ["", "Numbers in this brief are computed in code and cited; the refs identify each one."]
    return "\n".join(lines)


def as_slack(view: RunView, console_url: str) -> dict:
    """Slack's block format. Short on purpose: the detail lives in the console, one click away."""
    brief = view.brief
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": subject(view)}}]
    if brief is None:
        return {"text": subject(view), "blocks": blocks}

    if brief.freshness_line:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": brief.freshness_line}]})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": brief.summary}})

    for item in brief.items[:MAX_SLACK_ITEMS]:
        mark = "🔴" if item.severity == "HIGH" else "🟠"
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"{mark} *{item.headline}*\n{item.why}"}})
    if len(brief.items) > MAX_SLACK_ITEMS:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"…and {len(brief.items) - MAX_SLACK_ITEMS} more in the console."}]})

    pending = view.pending
    if pending:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                       f"*{len(pending)} waiting for approval.* <{_link(console_url, view)}|Open the "
                       "console> to approve, edit or reject."}})
    return {"text": subject(view), "blocks": blocks}


def _link(console_url: str, view: RunView) -> str:
    return f"{console_url.rstrip('/')}/?thread={view.thread_id}"
