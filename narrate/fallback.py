"""The brief that always ships (principle 7).

No model, no network, no chance of an invented number: the pack's facts are poured into sentences by code.
It reads flatter than the narrated version and it is never wrong, which is the right trade when the
alternative is a missing brief. A day with no brief is a silent failure — nobody notices that nothing arrived
until the week somebody needed it.

Two things this is *not*. It is not a degraded copy of the narration with the numbers dropped: every claim
carries the same citation the narrated version would, so the validator passes it unchanged and the console
renders it identically. And it is not a secret: `fallback_reason` is recorded on the run and reported in the
eval, because a fallback rate nobody publishes is a fallback rate that quietly becomes 100%.
"""

from __future__ import annotations

from narrate.contract import Brief, BriefItem, Claim
from pack.schema import EvidencePack, PackItem

DIRECTION_WORDS = {"down": "fell", "up": "rose"}


def templated_brief(pack: EvidencePack) -> Brief:
    """Build a brief straight from the pack. Every sentence is assembled from facts that already exist."""
    if pack.is_quiet:
        return Brief(summary=_quiet_summary(pack), freshness_line=_freshness_line(pack), items=[])
    return Brief(summary=_summary(pack), freshness_line=_freshness_line(pack),
                 items=[_item(pack, item) for item in pack.items])


def _summary(pack: EvidencePack) -> str:
    high = [i for i in pack.items if i.severity == "HIGH"]
    parts = [f"{len(pack.items)} item{'s' if len(pack.items) != 1 else ''} to review for "
             f"{pack.run_date.isoformat()}"]
    if high:
        parts.append(f"{len(high)} at HIGH: " + ", ".join(f"{i.metric_label} at {i.segment_label}"
                                                          for i in high))
    if pack.items_considered > len(pack.items):
        parts.append(f"{pack.items_considered - len(pack.items)} further item"
                     f"{'s' if pack.items_considered - len(pack.items) != 1 else ''} were held back by the "
                     f"limit of {pack.items_cap} per brief")
    if pack.calendar_note:
        parts.append(pack.calendar_note)
    return ". ".join(parts) + "."


def _quiet_summary(pack: EvidencePack) -> str:
    return (f"Nothing to review for {pack.run_date.isoformat()}: "
            f"{pack.quiet_reason or 'nothing above the reporting level'}."
            + (f" {pack.calendar_note}" if pack.calendar_note else ""))


def _freshness_line(pack: EvidencePack) -> str | None:
    """Said whether or not anything is wrong with the data, because that is the point of disclosing it."""
    if not pack.freshness:
        return None
    stale = [f for f in pack.freshness if f.stale]
    if stale:
        parts = [f"{s.source} are {pack.facts[s.days_ref].display} old (to {s.latest.isoformat()})"
                 for s in stale]
        return "Stale data: " + "; ".join(parts) + ". Treat the items below with that in mind."
    latest = max(f.latest for f in pack.freshness)
    return f"Data is current to {latest.isoformat()}."


def _item(pack: EvidencePack, item: PackItem) -> BriefItem:
    value = pack.facts[item.value_ref]
    expected = pack.facts.get(item.expected_ref) if item.expected_ref else None
    verb = DIRECTION_WORDS.get(item.direction, "moved")

    headline = f"{item.metric_label} at {item.segment_label}: {value.display}"
    claims = [Claim(text=f"{item.metric_label} for {item.segment_label} was {value.display} on "
                         f"{item.window_end.isoformat()}.", metric_ref=item.value_ref)]
    why = [f"{item.metric_label} {verb} to {value.display}"]

    if expected is not None and item.expected_ref is not None:
        claims.append(Claim(text=f"It normally runs at {expected.display}.", metric_ref=item.expected_ref))
        why.append(f"against a normal of {expected.display}")

    sentences = [" ".join(why) + "."]
    if item.detectors:
        sentences.append(f"Flagged because {item.detectors[0].detail}.")
    if item.drivers:
        driver = item.drivers[0]
        share = pack.facts[driver.share_ref]
        claims.append(Claim(
            text=f"{driver.label} accounts for {share.display} of the change.",
            metric_ref=driver.share_ref))
        sentences.append(f"{driver.label} accounts for {share.display} of the change, "
                         f"{pack.facts[driver.baseline_ref].display} to "
                         f"{pack.facts[driver.value_ref].display}.")
        claims.append(Claim(text=f"{driver.label} was at {pack.facts[driver.value_ref].display} over the "
                                 f"window.", metric_ref=driver.value_ref))
        claims.append(Claim(
            text=f"{driver.label} normally runs at {pack.facts[driver.baseline_ref].display}.",
            metric_ref=driver.baseline_ref))
    if item.driver_note and not item.drivers:
        sentences.append(item.driver_note.capitalize() + ".")
    if item.policy_note:
        sentences.append(f"Policy held this at {item.severity}: {item.policy_note}.")

    return BriefItem(id=item.id, headline=headline[:120], why=" ".join(sentences)[:600],
                     severity=item.severity, claims=claims)
