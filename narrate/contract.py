"""What the model is allowed to say, as a type.

The brief is not prose with citations bolted on; it is a structure in which **a claim cannot exist without a
reference**. `Claim.metric_ref` is required, so "the model forgot to cite" is not a failure mode the validator
has to catch — it is a shape the model cannot return. What the validator still has to catch is the harder
thing: a claim that cites a real fact and then says a different number.

The schema is sent to the provider as a strict JSON schema, so the structure is guaranteed by constrained
decoding. Everything below that line — the number in the text matching the fact it cites, no item without a
claim, the freshness line when a source is stale — is checked in code, because a schema cannot express it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["WARN", "HIGH"]
MAX_HEADLINE = 120
MAX_CLAIM = 240


class Claim(BaseModel):
    """One sentence that says a number, and the fact it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(max_length=MAX_CLAIM)
    metric_ref: str


class BriefItem(BaseModel):
    """One incident, written up. `id` ties it back to the pack item it is about."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    headline: str = Field(max_length=MAX_HEADLINE)
    why: str = Field(max_length=600)
    severity: Severity
    claims: list[Claim] = Field(min_length=1)


class Brief(BaseModel):
    """A morning's brief as the model returns it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(max_length=600)
    freshness_line: str | None = None      # an empty string from the model means "nothing to disclose"
    items: list[BriefItem] = Field(default_factory=list)

    @property
    def claims(self) -> list[Claim]:
        return [claim for item in self.items for claim in item.claims]


def brief_schema() -> dict:
    """The strict JSON schema sent to the provider.

    Written out rather than generated from the model, because strict mode is unforgiving about the dialect it
    accepts (no `$defs` indirection, every property required, `additionalProperties` false everywhere) and a
    schema that fails at the provider is a failure the fallback has to absorb for no reason.
    """
    claim = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string",
                     "description": "One sentence a person would read. Say the number exactly as the fact "
                                    "gives it."},
            "metric_ref": {"type": "string",
                           "description": "The ref of the fact this sentence's number came from, e.g. "
                                          "I1.value. It must be one of the refs in the pack."},
        },
        "required": ["text", "metric_ref"],
    }
    item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string", "description": "The pack item's id, e.g. I1."},
            "headline": {"type": "string", "description": "What happened, in under twelve words."},
            "why": {"type": "string",
                    "description": "Why it matters and what the evidence says, in two or three sentences. "
                                   "Every number here must also appear in a claim."},
            "severity": {"type": "string", "enum": ["WARN", "HIGH"],
                         "description": "Copy the item's severity. Do not decide it yourself."},
            "claims": {"type": "array", "minItems": 1, "items": claim},
        },
        "required": ["id", "headline", "why", "severity", "claims"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string",
                        "description": "Two or three sentences for someone who reads nothing else."},
            # A union type here ("string" or "null") is rejected outright by strict mode on some
            # providers — measured: it cost one brief in the first narration sample, which fell back for a
            # reason that had nothing to do with its writing. An empty string says "nothing to disclose".
            "freshness_line": {"type": "string",
                               "description": "Required when any source is stale: say which, and how old. "
                                              "Empty string when everything is current."},
            "items": {"type": "array", "items": item},
        },
        "required": ["summary", "freshness_line", "items"],
    }


def parse_brief(payload: dict) -> Brief:
    return Brief.model_validate(payload)
