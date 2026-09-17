"""Draft the actions a brief proposes, from the pack, in code.

The bodies are assembled from facts, not written by the model, for the same reason the numbers are: a ticket
is a durable artefact that someone will read next month without the brief beside it, and a paraphrased number
in a ticket is a wrong number with a long life. Every action carries the refs it was built from, so the ticket
says where each figure came from.

What decides the *type* is the anomaly, through `policy.yaml`: the first allowed action for that anomaly type,
in the order the policy lists them, which makes the policy file the thing to edit when the business wants
tickets instead of investigations. The gate then checks the result anyway — see `act/policy_gate.py` — because
a drafter that can only produce allowed actions is one refactor away from producing a disallowed one.
"""

from __future__ import annotations

from datetime import date

from act.models import Action, ActionType
from pack.schema import EvidencePack, PackItem

# Who should look at each kind of problem. A hint, not an assignment: the tracker decides, and a person can
# change it before approving.
ASSIGNEES: dict[str, str] = {
    "otif_drop": "plant operations lead",
    "fill_rate_drop": "plant operations lead",
    "yield_variance": "production manager",
    "inventory_at_risk": "distribution centre supervisor",
    "backlog_build": "customer service lead",
}
SEVERITY_WORDS = {"HIGH": "needs attention today", "WARN": "worth a look"}


def draft_actions(pack: EvidencePack, policy: dict) -> list[Action]:
    """One action per item, of a type the policy allows for that anomaly, capped by the policy's limit."""
    allowed = policy["allowed_actions"]
    limit = int(policy["limits"]["max_actions_per_brief"])
    actions: list[Action] = []

    for item in pack.items:
        choices: list[str] = list(allowed.get(item.anomaly_type, []))
        if not choices:
            continue                      # policy allows nothing here: propose nothing, and say so in the log
        action_type = _preferred(choices, item)
        actions.append(Action(
            id=f"{pack.run_date.isoformat()}-{item.id}-{action_type}",
            item_id=item.id, anomaly_type=item.anomaly_type, type=action_type,  # type: ignore[arg-type]
            title=_title(item, pack), body=_body(item, pack),
            assignee_hint=ASSIGNEES.get(item.anomaly_type, "supply chain team"),
            evidence_refs=_refs(item)))
    return actions[:limit]


def _preferred(choices: list[str], item: PackItem) -> str:
    """A HIGH item takes the policy's first choice; a WARN takes the least intrusive one it allows.

    Both come from the same list in policy.yaml — the business decides what may be raised, and severity only
    decides how loudly. Nobody wants a ticket raised for every WARN, and nobody wants a HIGH to arrive as a
    note somebody reads on Thursday.
    """
    if item.severity == "HIGH":
        return choices[0]
    quiet = [c for c in ("internal_update", "investigation") if c in choices]
    return quiet[0] if quiet else choices[0]


def _title(item: PackItem, pack: EvidencePack) -> str:
    value = pack.facts[item.value_ref].display
    verb = "down to" if item.direction == "down" else "up to"
    return f"[{item.severity}] {item.metric_label} {verb} {value} at {item.segment_label}"[:120]


def _body(item: PackItem, pack: EvidencePack) -> str:
    """Everything a reader needs to act without the brief in front of them, and nothing they cannot check."""
    value = pack.facts[item.value_ref]
    lines = [
        f"{item.metric_label} at {item.segment_label} was {value.display} on "
        f"{item.window_end.isoformat()} ({SEVERITY_WORDS.get(item.severity, 'flagged')}).",
        "",
        f"- The number: {value.display} ({value.ref})",
    ]
    if item.expected_ref:
        expected = pack.facts[item.expected_ref]
        lines.append(f"- Normal for this segment: {expected.display} ({expected.ref})")
    if item.delta_ref:
        lines.append(f"- Movement: {pack.facts[item.delta_ref].display} ({item.delta_ref})")
    lines.append(f"- Volume behind it: {pack.facts[item.volume_ref].display} ({item.volume_ref})")
    lines.append(f"- Window: {item.window_start.isoformat()} to {item.window_end.isoformat()}")
    lines.append("")
    lines.append("Why it was flagged:")
    lines += [f"- {note.detail} ({note.detector})" for note in item.detectors]

    if item.drivers:
        lines += ["", "Where it is coming from:"]
        for driver in item.drivers:
            share = pack.facts[driver.share_ref].display
            lines.append(f"- {driver.label}: {share} of the change, "
                         f"{pack.facts[driver.baseline_ref].display} to "
                         f"{pack.facts[driver.value_ref].display} ({driver.share_ref})")
    if item.driver_note:
        lines += ["", f"Note: {item.driver_note}"]
    if item.policy_note:
        lines += ["", f"Policy note: {item.policy_note}"]
    if item.stale:
        lines += ["", "The data behind this was stale when the brief ran; check the dates before acting."]

    lines += ["", f"Raised from the brief for {pack.run_date.isoformat()}. "
                  "Numbers are computed in code; the refs in brackets identify each one."]
    return "\n".join(lines)


def _refs(item: PackItem) -> list[str]:
    refs = [item.value_ref, item.expected_ref, item.delta_ref, item.volume_ref]
    refs += [driver.share_ref for driver in item.drivers]
    return [ref for ref in refs if ref]


def suppression_key(anomaly_type: str, segment_label: str, metric: str) -> str:
    """What counts as "the same problem" for duplicate suppression."""
    return f"{anomaly_type}|{metric}|{segment_label}"


def key_for(action: Action, pack: EvidencePack) -> str:
    item = next((i for i in pack.items if i.id == action.item_id), None)
    if item is None:
        return suppression_key(action.anomaly_type, "", "")
    return suppression_key(action.anomaly_type, item.segment_label, item.metric)


def as_type(value: str) -> ActionType:
    if value not in ("jira_ticket", "investigation", "internal_update"):
        raise ValueError(f"unknown action type {value!r}")
    return value  # type: ignore[return-value]


def drafted_on(pack: EvidencePack) -> date:
    return pack.run_date
