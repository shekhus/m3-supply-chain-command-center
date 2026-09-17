"""Narrate a pack: one attempt, one correction, then the templated brief. The brief always ships.

The retry is the interesting part. A model that produced an uncited number is not helped by being told to try
again — it is helped by being told *what it wrote and what the fact says*, which is exactly what the validator
produces. So the second attempt carries the complaints verbatim, and nothing else changes: same pack, same
prompt, same model.

There is no third attempt. Past two, the honest options are a flatter brief that is certainly right or an
argument with a language model at six in the morning, and only one of those ships.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from llm.client import InvalidOutput, LLMClient, LLMError, load_prompt
from narrate.contract import Brief, brief_schema, parse_brief
from narrate.fallback import templated_brief
from narrate.validate import ValidationResult, validate
from pack.schema import EvidencePack

Source = Literal["llm", "llm_retry", "fallback"]
# A six-item brief with a claim per number runs past 4,000 output tokens, and the request comes back
# truncated with finish_reason=length — measured: one brief in the first narration sample fell back for
# running out of room rather than for anything it wrote (docs/decisions.md B-008).
MAX_TOKENS = 8000


@dataclass
class NarrationResult:
    brief: Brief
    source: Source
    validation: ValidationResult
    fallback_reason: str | None = None
    attempts: int = 0
    rejected: list[Brief] = field(default_factory=list)   # drafts the validator turned down, for the record

    @property
    def used_fallback(self) -> bool:
        return self.source == "fallback"

    def to_dict(self) -> dict:
        return {"source": self.source, "attempts": self.attempts,
                "fallback_reason": self.fallback_reason,
                "citation_coverage": self.validation.citation_coverage,
                "complaints": [str(c) for c in self.validation.complaints],
                "claims": self.validation.claims, "numbers_checked": self.validation.numbers_checked}


def narrate(pack: EvidencePack, client: LLMClient | None, batch_id: str | None = None) -> NarrationResult:
    """Narrate the pack, or fall back to the templated brief. Never raises for an LLM failure."""
    if client is None:
        return _fallback(pack, "no model configured (LLM_PROVIDER=none)", attempts=0)

    system = load_prompt("narrate.md")
    user = _user_message(pack)
    complaints: str | None = None
    rejected: list[Brief] = []

    for attempt in (1, 2):
        try:
            brief = client.complete_json(
                purpose="narrate" if attempt == 1 else "narrate_retry",
                system=system, user=user if complaints is None else _with_complaints(user, complaints),
                schema=brief_schema(), parse=parse_brief, max_tokens=MAX_TOKENS, batch_id=batch_id)
        except (LLMError, InvalidOutput) as exc:
            if attempt == 2:
                return _fallback(pack, f"model failed twice: {exc}"[:500], attempts=2)
            complaints = f"The provider rejected or mangled the last answer: {exc}"
            continue

        result = validate(brief, pack)
        if result.ok:
            return NarrationResult(brief=brief, source="llm" if attempt == 1 else "llm_retry",
                                   validation=result, attempts=attempt, rejected=rejected)
        rejected.append(brief)
        if attempt == 2:
            return _fallback(pack, f"validator rejected both attempts: {result.message()}"[:500], attempts=2,
                             last=result, rejected=rejected)
        complaints = result.message()

    raise AssertionError("unreachable")            # pragma: no cover - the loop returns on every path


def _fallback(pack: EvidencePack, reason: str, attempts: int, last: ValidationResult | None = None,
              rejected: list[Brief] | None = None) -> NarrationResult:
    """The templated brief, validated like any other: it is the one that has to be right."""
    brief = templated_brief(pack)
    checked = validate(brief, pack)
    if not checked.ok:                             # pragma: no cover - a test asserts this never happens
        raise AssertionError(f"the templated brief does not validate: {checked.message()}")
    return NarrationResult(brief=brief, source="fallback", validation=checked, fallback_reason=reason,
                           attempts=attempts, rejected=list(rejected or []))


def _user_message(pack: EvidencePack) -> str:
    return ("Write today's brief from this evidence pack. Every number must be copied from `facts` and "
            "cited.\n\n" + json.dumps(pack.for_prompt(), indent=1, sort_keys=False))


def _with_complaints(user: str, complaints: str) -> str:
    """The validator's own words, handed back. "You said X, the fact says Y" beats "try again"."""
    return (f"{user}\n\nYour previous answer was rejected. Fix exactly these problems and change nothing "
            f"else:\n{complaints}\n\nEvery number must be copied character for character from the fact you "
            "cite. If a number you want is not in `facts`, leave it out.")
