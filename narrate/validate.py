"""The fact-checker standing behind the writer.

Five rules, all enforced in code against the pack, never by asking the model whether it was careful:

1. **Every claim cites a fact that exists.** A `metric_ref` that is not in the pack is a made-up citation, and
   a made-up citation is worse than no citation because it reads as rigour.
2. **Every number in a claim matches a fact the claim cites.** This is the rule that matters. A model that
   cites `I1.value` correctly and then writes "74.8%" instead of "74.1%" has produced a sentence that is
   wrong, cited, and completely convincing. Numbers are extracted from the text and each one must equal a
   cited fact, within the tolerance of the rounding the pack already did.
3. **Every item is one of the pack's items, and says what the pack says.** No invented incidents, no promoting
   a WARN to HIGH for emphasis.
4. **Freshness is disclosed.** If any source is stale the brief must say so; the rule exists because a brief
   that narrates over stale data is worse than no brief (principle 3).
5. **Nothing outside the pack is named.** A segment the reader may not see cannot appear, because it was never
   in the pack — but a model can still invent a plant name, so the check is made explicitly.

The result is a list of complaints in plain language. That list is what gets sent back to the model for its
one regeneration: "you said X, the fact says Y" is a far better instruction than "try again".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from narrate.contract import Brief, Claim
from pack.schema import EvidencePack, Fact

# Numbers as a person writes them: 74.1%, 1,240 lb, -5.4 points, 3 days. The unit is kept so that "3" in
# "3 days running" is not compared against a percentage that happens to round to 3.
NUMBER = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)\s*(%|percentage points|points|pounds|lb|days|day)?")
# Dates hold digits without being quantities, and so do the names the pack uses for segments. Both are
# masked before numbers are extracted, because the "01" inside PLT-01 is not a claim about anything. The
# names are taken from the pack rather than guessed at with a regex: the first version of this tried to
# describe identifier shapes, and the narration sample found the spellings it had not thought of.
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# Typographic characters a writer reaches for and a string comparison does not expect. A model that writes
# PLT‑02 with a non-breaking hyphen has written the plant's name; a validator that does not normalise
# first sees a bare "02" and rejects the sentence. Measured: this was three of the four fallbacks in the
# second narration sample (docs/decisions.md B-008).
LOOKALIKES = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", " ": " ", " ": " ", " ": " ", " ": " ", "’": "'",
})


def normalise(text: str) -> str:
    """ASCII dashes and ordinary spaces: the same sentence, in characters a comparison can recognise."""
    return text.translate(LOOKALIKES)
# Words that carry a number without being one: these may appear without a citation.
SAFE_WORDS = {"one", "two", "three", "first", "second", "third"}
TOLERANCE = 0.051          # the pack rounds to one decimal place; a claim may not drift further than that


@dataclass
class Complaint:
    """One thing wrong, in the words the model will be shown."""

    rule: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.where}: {self.detail}"


@dataclass
class ValidationResult:
    complaints: list[Complaint] = field(default_factory=list)
    claims: int = 0
    cited_claims: int = 0
    numbers_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.complaints

    @property
    def citation_coverage(self) -> float:
        """Claims carrying a resolvable reference, over all claims. The hard gate: this must be 1.0."""
        return self.cited_claims / self.claims if self.claims else 1.0

    def message(self) -> str:
        return "\n".join(str(c) for c in self.complaints)


def validate(brief: Brief, pack: EvidencePack) -> ValidationResult:
    result = ValidationResult(claims=len(brief.claims))
    by_id = {item.id: item for item in pack.items}

    for item in brief.items:
        where = f"item {item.id}"
        source = by_id.get(item.id)
        if source is None:
            result.complaints.append(Complaint(
                "unknown-item", where,
                f"there is no item {item.id} in the evidence pack; the items are {sorted(by_id) or 'none'}"))
            continue
        if item.severity != source.severity:
            result.complaints.append(Complaint(
                "severity-changed", where,
                f"you wrote {item.severity}; the pack says {source.severity}. Severity is decided in code."))
        phrases = _pack_phrases(source, pack)
        _check_text(item.headline, f"{where} headline", item.claims, pack, result, phrases)
        _check_text(item.why, f"{where} explanation", item.claims, pack, result, phrases)
        for index, claim in enumerate(item.claims, start=1):
            _check_claim(claim, f"{where} claim {index}", pack, result, phrases)

    _check_summary(brief, pack, result)
    _check_freshness(brief, pack, result)
    _check_invented_segments(brief, pack, result)
    return result


def _names(pack: EvidencePack) -> tuple[str, ...]:
    """Every name the pack uses for a thing, longest first so PLT-02 C000031 masks before PLT-02 does."""
    names = {item.segment_label for item in pack.items}
    names |= {part for label in list(names) for part in label.split()}
    names |= {d.label for item in pack.items for d in item.drivers}
    names |= {part for item in pack.items for d in item.drivers for part in d.label.split()}
    return tuple(sorted((n for n in names if any(ch.isdigit() for ch in n)), key=len, reverse=True))


def _pack_phrases(item, pack: EvidencePack) -> tuple[str, ...]:  # noqa: ANN001 - pack.schema.PackItem
    """The strings the pack itself wrote for this item, which a brief is allowed to quote verbatim."""
    parts = [note.detail for note in item.detectors]
    parts += [p for p in (item.driver_note, item.policy_note, pack.calendar_note) if p]
    return tuple(parts)


def _check_claim(claim: Claim, where: str, pack: EvidencePack, result: ValidationResult,
                 phrases: tuple[str, ...] = ()) -> None:
    fact = pack.fact(claim.metric_ref)
    if fact is None:
        result.complaints.append(Complaint(
            "unknown-ref", where,
            f"'{claim.metric_ref}' is not a fact in the pack. Cite one of the refs you were given."))
        return
    result.cited_claims += 1
    _check_text(claim.text, where, [claim], pack, result, phrases)


def _check_text(text: str, where: str, claims: list[Claim], pack: EvidencePack, result: ValidationResult,
                phrases: tuple[str, ...] = ()) -> None:
    """Every number here must match a fact cited nearby, or be one the pack itself wrote.

    The second case is not a loophole. `why_flagged` ("20.0 points below its own normal for 2 days
    running") is rendered in code from the detector's own output, and a brief forbidden from repeating
    the reason something was flagged cannot explain anything. The number still came from code: it is
    quoted rather than cited, and it has to appear verbatim in a string the pack supplied for this item.
    """
    cited = [pack.fact(c.metric_ref) for c in claims]
    available = [f for f in cited if f is not None]
    for raw, unit, span in _numbers(text, _names(pack)):
        result.numbers_checked += 1
        if _matches_any(raw, unit, available) or any(span in normalise(p) for p in phrases):
            continue
        result.complaints.append(Complaint(
            "number-not-in-pack", where,
            f"you wrote '{_render(raw, unit)}', which is not any of the facts cited here "
            f"({', '.join(f'{f.ref}={f.display}' for f in available) or 'none'}). "
            "Say the number exactly as the fact gives it, or cite the fact it came from."))


def _check_summary(brief: Brief, pack: EvidencePack, result: ValidationResult) -> None:
    """Numbers a claim has already cited, phrases the pack wrote, or a count of the brief's own items.

    "3 items to review" is a statement about the page in the reader's hand, not a measurement of the
    business, and there is nothing for it to be wrong about.
    """
    counts = tuple(str(n) for n in {len(pack.items), pack.items_considered, pack.items_cap,
                                    sum(1 for i in pack.items if i.severity == "HIGH"),
                                    max(pack.items_considered - len(pack.items), 0)})
    phrases = counts + tuple(p for p in [pack.calendar_note, pack.quiet_reason] if p)
    _check_text(brief.summary, "summary", brief.claims, pack, result, phrases)


def _check_freshness(brief: Brief, pack: EvidencePack, result: ValidationResult) -> None:
    if not pack.any_stale:
        return
    stale = [f for f in pack.freshness if f.stale]
    line = brief.freshness_line or ""
    if not line.strip():
        result.complaints.append(Complaint(
            "freshness-missing", "brief",
            f"{', '.join(s.source for s in stale)} {'is' if len(stale) == 1 else 'are'} stale and the brief "
            "does not say so. Open with how old the data is."))
        return
    facts = [f for f in (pack.fact(s.days_ref) for s in stale) if f is not None]
    _check_numbers_against(line, facts, "freshness line", result)


def _check_numbers_against(text: str, facts: list[Fact], where: str, result: ValidationResult) -> None:
    for raw, unit, _span in _numbers(text):
        result.numbers_checked += 1
        if not _matches_any(raw, unit, facts):
            result.complaints.append(Complaint(
                "number-not-in-pack", where,
                f"you wrote '{_render(raw, unit)}'; the facts here are "
                f"{', '.join(f'{f.ref}={f.display}' for f in facts)}."))


def _check_invented_segments(brief: Brief, pack: EvidencePack, result: ValidationResult) -> None:
    """A plant or customer the pack never mentioned. Cheap to check, and the failure is severe."""
    known = {item.segment_label for item in pack.items}
    known |= {part for label in known for part in label.split()}
    known |= {d.label for item in pack.items for d in item.drivers}
    known |= {part for item in pack.items for d in item.drivers for part in d.label.split()}
    pattern = re.compile(r"\b(PLT-\d+|C\d{6}|DC-[A-Z]+)\b")
    for item in brief.items:
        for raw_text, where in ((item.headline, "headline"), (item.why, "explanation"),
                                *[(c.text, f"claim {n}") for n, c in enumerate(item.claims, start=1)]):
            text = normalise(raw_text)
            for name in pattern.findall(text):
                if name not in known:
                    result.complaints.append(Complaint(
                        "unknown-segment", f"item {item.id} {where}",
                        f"'{name}' is not in this brief's evidence. Only write about what you were given."))


def _numbers(text: str, names: tuple[str, ...] = ()) -> list[tuple[str, str | None, str]]:
    """Quantities in a piece of text, with dates and the pack's own segment names masked out first."""
    masked = ISO_DATE.sub(lambda m: "#" * len(m.group(0)), normalise(text))
    for name in names:
        masked = masked.replace(name, "#" * len(name))
    out = []
    for match in NUMBER.finditer(masked):
        raw = match.group(1)
        if raw.strip("-") in SAFE_WORDS:
            continue
        out.append((raw, match.group(2), match.group(0).strip()))
    return out


def _matches_any(raw: str, unit: str | None, facts: list[Fact]) -> bool:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return True                       # not a number after all; nothing to check
    for fact in facts:
        if _matches(value, unit, fact):
            return True
    return False


def _matches(value: float, unit: str | None, fact: Fact) -> bool:
    """Does this written number mean the same thing as the fact?

    Compared against the *displayed* number rather than the stored one: the pack already decided that 0.74138
    is written "74.1%", so a claim saying 74.1 is right and one saying 74.138 is not — it is a number the
    reader cannot check against anything they will ever be shown.

    A change may be written without its sign. "OTIF fell by 9.4 points" is how a person writes a change of
    −9.4, and rejecting it would be a validator enforcing notation rather than truth; the direction is carried
    by the sentence and by the item's own `direction`, which the reader sees. The magnitude must match.
    """
    shown = _displayed_number(fact)
    if shown is None:
        return False
    if abs(value - shown) <= TOLERANCE or abs(abs(value) - abs(shown)) <= TOLERANCE:
        return _unit_agrees(unit, fact)
    return False


def _unit_agrees(unit: str | None, fact: Fact) -> bool:
    if unit is None:
        return True
    unit = unit.lower()
    allowed = {
        "rate": {"%"},
        "percent": {"%"},
        "rate_change": {"points", "percentage points"},
        "points": {"points", "percentage points"},
        "pounds": {"lb", "pounds"},
        "days": {"day", "days"},
        "count": set(),
        "date": set(),
    }[fact.unit]
    return unit in allowed if allowed else False


def _displayed_number(fact: Fact) -> float | None:
    """The number as the pack writes it: 74.1 for "74.1%", 1240 for "1,240 lb"."""
    match = NUMBER.search(fact.display)
    if match is None:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _render(raw: str, unit: str | None) -> str:
    """As the writer wrote it, so the complaint quotes them rather than paraphrasing."""
    if unit is None:
        return raw
    return f"{raw}{unit}" if unit == "%" else f"{raw} {unit}"
