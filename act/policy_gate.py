"""The gate: what may be proposed to a person at all.

It runs on every action regardless of where the action came from — the drafter, a future model, a console
retry, a test. That is the point. A drafter that can only produce allowed actions is one refactor away from
producing a disallowed one, and the gate is the thing that will still be true afterwards.

Four rules, all from `policy.yaml`, none of them overridable from code or from a prompt:

1. **The action type must be allowed for that anomaly type.** "Adjust the price", "update M3", "release the
   hold" are not in the file, so they cannot reach a person. A refusal is recorded as a policy violation, not
   quietly dropped.
2. **A brief proposes at most `max_actions_per_brief`.** A person who is asked to approve fifteen things
   approves fifteen things without reading them.
3. **No duplicates of something already open.** The same lane, the same metric, a ticket raised four days ago
   and still open — raising a second one is how a tracker becomes noise.
4. **Nothing reaches a system of record without a person.** The gate marks actions `PROPOSED`, never
   `APPROVED`. There is no path through this module that executes anything (principle 5).

A blocked action is *kept*, with its reason, so the console can show what the system wanted to do and was not
allowed to. Silently dropping it would hide a policy that has drifted away from what the business needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from act.draft import key_for, suppression_key
from act.models import Action, ActionStatus, OpenAction
from pack.schema import EvidencePack


class PolicyError(RuntimeError):
    """The policy file itself is wrong. Never raised for a bad action — that is a refusal, not an error."""


@dataclass
class GateResult:
    """What the gate decided, in enough detail for the ops log and the console."""

    allowed: list[Action] = field(default_factory=list)
    blocked: list[Action] = field(default_factory=list)
    suppressed: list[Action] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def all_actions(self) -> list[Action]:
        return [*self.allowed, *self.blocked, *self.suppressed]

    def to_dict(self) -> dict:
        return {"allowed": [a.id for a in self.allowed],
                "blocked": [{"id": a.id, "reason": a.blocked_reason} for a in self.blocked],
                "suppressed": [{"id": a.id, "reason": a.blocked_reason} for a in self.suppressed],
                "violations": self.violations}


def load_gate_policy(policy: dict) -> dict[str, list[str]]:
    """The allow-list, checked for coherence once so a typo is an error rather than a silent refusal."""
    allowed = policy.get("allowed_actions")
    if not isinstance(allowed, dict) or not allowed:
        raise PolicyError("policy.yaml: allowed_actions is missing or empty")
    known = {"jira_ticket", "investigation", "internal_update"}
    for anomaly_type, types in allowed.items():
        if not isinstance(types, list) or not types:
            raise PolicyError(f"policy.yaml: allowed_actions.{anomaly_type} must be a non-empty list")
        unknown = [t for t in types if t not in known]
        if unknown:
            raise PolicyError(f"policy.yaml: allowed_actions.{anomaly_type} names unknown action types "
                              f"{unknown}; known types are {sorted(known)}")
    return {k: list(v) for k, v in allowed.items()}


def evaluate(actions: list[Action], pack: EvidencePack, policy: dict,
             open_actions: list[OpenAction] | None = None, today: date | None = None) -> GateResult:
    """Judge a set of proposed actions. Nothing is executed, and nothing is approved."""
    allow = load_gate_policy(policy)
    limits = policy["limits"]
    cap = int(limits["max_actions_per_brief"])
    window = int(limits["suppress_if_open_ticket_days"])
    as_of = today or pack.run_date
    already = _open_keys(open_actions or [], window, as_of)

    result = GateResult()
    for action in actions:
        permitted = allow.get(action.anomaly_type)
        if permitted is None:
            reason = (f"policy.yaml allows no actions for '{action.anomaly_type}'; "
                      f"it lists {sorted(allow)}")
            result.blocked.append(action.with_status(ActionStatus.BLOCKED, blocked_reason=reason))
            result.violations.append(f"{action.id}: {reason}")
            continue
        if action.type not in permitted:
            reason = (f"'{action.type}' is not allowed for {action.anomaly_type}; "
                      f"policy.yaml allows {permitted}")
            result.blocked.append(action.with_status(ActionStatus.BLOCKED, blocked_reason=reason))
            result.violations.append(f"{action.id}: {reason}")
            continue
        if action.item_id not in {item.id for item in pack.items}:
            reason = f"there is no item {action.item_id} in this brief's evidence"
            result.blocked.append(action.with_status(ActionStatus.BLOCKED, blocked_reason=reason))
            result.violations.append(f"{action.id}: {reason}")
            continue

        key = key_for(action, pack)
        if key in already:
            reason = (f"an action for this is already open ({already[key]}); policy suppresses duplicates "
                      f"within {window} days")
            result.suppressed.append(action.with_status(ActionStatus.SUPPRESSED, blocked_reason=reason))
            continue
        if len(result.allowed) >= cap:
            reason = f"the brief's limit of {cap} actions was reached before this one"
            result.suppressed.append(action.with_status(ActionStatus.SUPPRESSED, blocked_reason=reason))
            continue

        already[key] = action.id          # a brief does not propose the same thing twice either
        result.allowed.append(action.with_status(ActionStatus.PROPOSED))
    return result


def _open_keys(open_actions: list[OpenAction], window_days: int, as_of: date) -> dict[str, str]:
    """Anything still open and recent enough to make a second one a duplicate."""
    cutoff = as_of - timedelta(days=window_days)
    out: dict[str, str] = {}
    for open_action in open_actions:
        if open_action.created_on < cutoff:
            continue
        if open_action.status in {ActionStatus.REJECTED, ActionStatus.BLOCKED}:
            continue
        key = suppression_key(open_action.anomaly_type, open_action.segment_label, open_action.metric)
        out.setdefault(key, open_action.external_ref or open_action.status.value)
    return out


def check_decision(action: Action, verdict: str, policy: dict) -> None:
    """A person's approval is still bound by the policy they are approving under.

    The console cannot be the place this is enforced: an editor who changes an action's type, or an API call
    made directly, would then walk straight past the allow-list. Approval means "yes, do the thing the policy
    permits", not "yes, do anything".
    """
    if verdict not in ("approve", "edit", "reject"):
        raise PolicyError(f"unknown verdict {verdict!r}")
    if verdict == "reject":
        return
    allow = load_gate_policy(policy)
    permitted = allow.get(action.anomaly_type, [])
    if action.type not in permitted:
        raise PolicyError(f"'{action.type}' is not allowed for {action.anomaly_type}; "
                          f"policy.yaml allows {permitted}")
    if action.status is ActionStatus.BLOCKED:
        raise PolicyError(f"{action.id} was blocked by policy and cannot be approved: "
                          f"{action.blocked_reason}")
