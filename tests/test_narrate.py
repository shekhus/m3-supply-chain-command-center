"""The citation gate, tested with briefs built to get past it.

A validator is only worth its runtime if it fails the right things, so most of this file writes bad briefs on
purpose: the right citation with the wrong number, an invented reference, a severity talked up, a stale day
narrated over, a plant that does not exist. The templated fallback is then held to the same gate, because the
brief that ships when everything else fails is the one that has to be right.

No model is called here. The LLM path is exercised with a stub backend that returns whatever a test wants,
which is the only way to test "the model got it wrong twice" as a routine case rather than an outage.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.config import REPO_ROOT
from detect.config import load_detect_config
from detect.run import build_series, load_frames
from llm.client import Completion, InvalidOutput, LLMClient, LLMError, MemoryRecorder
from metrics.runner import load_policy
from narrate.contract import Brief, BriefItem, Claim, brief_schema, parse_brief
from narrate.fallback import templated_brief
from narrate.run import narrate
from narrate.validate import validate
from pack.build import build_pack
from pack.permissions import audience_for
from pack.schema import EvidencePack

POLICY = REPO_ROOT / "policy.yaml"
METRICS = REPO_ROOT / "data" / "metrics"
LANE_DAY = date(2025, 9, 15)


@pytest.fixture(scope="module")
def world() -> tuple:
    if not (METRICS / "daily_otif_total.parquet").exists():
        pytest.skip("data/ not generated (run `make synth-gold`)")
    cfg = load_detect_config(POLICY)
    policy = load_policy(POLICY)
    frames = load_frames(METRICS)
    return frames, cfg, policy, build_series(frames, cfg)


@pytest.fixture(scope="module")
def pack(world: tuple) -> EvidencePack:
    frames, cfg, policy, series = world
    built, _ = build_pack(frames, cfg, policy, LANE_DAY, series, audience_for("vp"))
    return built


def _good(pack: EvidencePack) -> Brief:
    return templated_brief(pack)


def _one_item(pack: EvidencePack, *, id: str | None = None, severity: str | None = None,
              headline: str | None = None, why: str | None = None,
              claims: list[Claim] | None = None) -> Brief:
    """A minimal valid brief about the pack's first item, with fields swapped in to break it."""
    item = pack.items[0]
    value = pack.facts[item.value_ref]
    text = f"{item.metric_label} was {value.display}."
    return Brief(summary="One item to review.", freshness_line="Data is current.", items=[BriefItem(
        id=id or item.id,
        severity=severity or item.severity,  # type: ignore[arg-type]
        headline=headline or f"{item.metric_label} at {item.segment_label}",
        why=why or text,
        claims=claims if claims is not None else [Claim(text=text, metric_ref=item.value_ref)])])


# --- the gate ---------------------------------------------------------------------------------


def test_the_templated_brief_passes_the_gate(pack: EvidencePack) -> None:
    result = validate(_good(pack), pack)
    assert result.ok, result.message()
    assert result.citation_coverage == 1.0
    assert result.claims >= 3 and result.numbers_checked > 0


def test_a_right_citation_with_a_wrong_number_is_caught(pack: EvidencePack) -> None:
    """The failure that matters: cited, confident, and wrong by half a point."""
    item = pack.items[0]
    real = pack.facts[item.value_ref].display
    wrong = f"{float(real.rstrip('%')) + 8:.1f}%"
    brief = _one_item(pack, why=f"It was {wrong}.",
                      claims=[Claim(text=f"It was {wrong}.", metric_ref=item.value_ref)])

    result = validate(brief, pack)
    assert not result.ok
    assert any(c.rule == "number-not-in-pack" for c in result.complaints)
    assert wrong in result.message() and real in result.message()   # the complaint shows both


def test_a_number_within_the_packs_own_rounding_is_accepted(pack: EvidencePack) -> None:
    """74.1% is the fact; a brief may not invent 74.138% precision, but it may say 74.1%."""
    item = pack.items[0]
    shown = pack.facts[item.value_ref].display
    assert validate(_one_item(pack, why=f"It was {shown}.",
                              claims=[Claim(text=f"It was {shown}.", metric_ref=item.value_ref)]),
                    pack).ok


def test_an_invented_reference_is_caught(pack: EvidencePack) -> None:
    brief = _one_item(pack, claims=[Claim(text="Something happened.", metric_ref="I9.value")])
    result = validate(brief, pack)
    assert not result.ok and any(c.rule == "unknown-ref" for c in result.complaints)
    assert "I9.value" in result.message()


def test_an_invented_item_is_caught(pack: EvidencePack) -> None:
    brief = _one_item(pack, id="I99")
    result = validate(brief, pack)
    assert not result.ok and any(c.rule == "unknown-item" for c in result.complaints)


def test_talking_up_the_severity_is_caught(pack: EvidencePack) -> None:
    """Severity is computed from policy. A writer emphasising is a writer deciding."""
    warn = next((i for i in pack.items if i.severity == "WARN"), None)
    assert warn is not None
    brief = _one_item(pack, id=warn.id, severity="HIGH",
                      claims=[Claim(text="It moved.", metric_ref=warn.value_ref)])
    result = validate(brief, pack)
    assert not result.ok and any(c.rule == "severity-changed" for c in result.complaints)


def test_a_segment_the_pack_never_mentioned_is_caught(pack: EvidencePack) -> None:
    """The permission filter keeps other plants out of the pack; this stops one being invented."""
    item = pack.items[0]
    brief = _one_item(pack, why="PLT-09 is also affected.",
                      claims=[Claim(text="PLT-09 is also affected.", metric_ref=item.value_ref)])
    result = validate(brief, pack)
    assert not result.ok and any(c.rule == "unknown-segment" for c in result.complaints)
    assert "PLT-09" in result.message()


def test_naming_a_segment_the_pack_does_mention_is_fine(pack: EvidencePack) -> None:
    item = next(i for i in pack.items if i.drivers)
    driver = item.drivers[0]
    brief = _one_item(pack, id=item.id, severity=item.severity,
                      why=f"{driver.label} accounts for {pack.facts[driver.share_ref].display}.",
                      claims=[Claim(text=f"{driver.label} accounts for "
                                         f"{pack.facts[driver.share_ref].display} of the change.",
                                    metric_ref=driver.share_ref)])
    assert validate(brief, pack).ok


def test_the_detectors_own_words_may_be_quoted(pack: EvidencePack) -> None:
    """"flagged because it was 20.0 points below normal" is the pack's sentence, not the model's."""
    item = pack.items[0]
    detail = item.detectors[0].detail
    brief = _one_item(pack, why=f"Flagged because {detail}.",
                      claims=[Claim(text=f"It was {pack.facts[item.value_ref].display}.",
                                    metric_ref=item.value_ref)])
    assert validate(brief, pack).ok


def test_identifiers_and_dates_are_not_mistaken_for_claims(pack: EvidencePack) -> None:
    """The "02" in PLT-02 and the "09" in a date are not numbers anybody can check a metric against."""
    item = pack.items[0]
    brief = _one_item(pack, why=f"On {LANE_DAY.isoformat()}, PLT-02 C000031 was affected.",
                      claims=[Claim(text=f"On {LANE_DAY.isoformat()} it was "
                                         f"{pack.facts[item.value_ref].display}.",
                                    metric_ref=item.value_ref)])
    assert validate(brief, pack).ok


def test_a_stale_source_must_be_disclosed(world: tuple) -> None:
    """Principle 3: narrating over stale data is worse than not narrating at all."""
    frames, cfg, policy, series = world
    stale_day = max(frames["daily_otif_total"]["metric_date"]) + timedelta(days=4)
    pack, _ = build_pack(frames, cfg, policy, stale_day, series, audience_for("vp"))
    assert pack.any_stale

    silent = Brief(summary="Nothing much.", freshness_line=None, items=[])
    result = validate(silent, pack)
    assert not result.ok and any(c.rule == "freshness-missing" for c in result.complaints)

    assert validate(templated_brief(pack), pack).ok      # the fallback discloses it


def test_the_gate_holds_across_a_sample_of_days(world: tuple) -> None:
    """The week-8 definition of done, on the path that never calls a model: 100% citation coverage."""
    frames, cfg, policy, series = world
    start = date(2026, 3, 2)
    checked = 0
    for index in range(20):
        day = start + timedelta(days=index * 7)
        for user in ("vp", "plt02"):
            pack, _ = build_pack(frames, cfg, policy, day, series, audience_for(user))
            result = validate(templated_brief(pack), pack)
            assert result.ok, f"{day} {user}: {result.message()}"
            assert result.citation_coverage == 1.0
            checked += 1
    assert checked == 40


# --- the contract -----------------------------------------------------------------------------


def test_a_claim_cannot_exist_without_a_reference() -> None:
    """Not a validator rule: a shape the model cannot return."""
    with pytest.raises(ValueError, match="metric_ref"):
        Claim(text="OTIF fell.")          # type: ignore[call-arg]


def test_an_item_cannot_exist_without_a_claim() -> None:
    with pytest.raises(ValueError, match="claims"):
        BriefItem(id="I1", headline="x", why="y", severity="HIGH", claims=[])


def test_the_schema_is_strict_everywhere() -> None:
    """Strict mode is unforgiving; a schema the provider rejects is a fallback nobody needed."""
    schema = brief_schema()

    def walk(node: dict) -> None:
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
            assert set(node.get("required", [])) == set(node.get("properties", {}))
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])

    walk(schema)
    assert json.dumps(schema)


def test_unexpected_fields_are_refused() -> None:
    with pytest.raises(ValueError, match="extra"):
        parse_brief({"summary": "s", "freshness_line": None, "items": [], "confidence": 0.9})


# --- the model path, with a stub ---------------------------------------------------------------


class StubBackend:
    """Answers with whatever a test queued. Failure is a value here, not an outage."""

    provider = "stub"
    model = "stub-1"

    def __init__(self, *answers: str | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, schema: dict, max_tokens: int) -> Completion:
        self.calls.append((system, user))
        answer = self.answers.pop(0) if self.answers else '{"summary":"","freshness_line":null,"items":[]}'
        if isinstance(answer, Exception):
            raise answer
        return Completion(answer, 100, 50, "stop")


def _client(*answers: str | Exception) -> tuple[LLMClient, StubBackend, MemoryRecorder]:
    backend = StubBackend(*answers)
    recorder = MemoryRecorder()
    return LLMClient(backend, recorder, 0.0, 0.0), backend, recorder


def _as_json(brief: Brief) -> str:
    return brief.model_dump_json()


def test_a_good_answer_is_used_as_is(pack: EvidencePack) -> None:
    client, backend, recorder = _client(_as_json(_good(pack)))
    result = narrate(pack, client)
    assert result.source == "llm" and result.attempts == 1 and result.validation.ok
    assert len(backend.calls) == 1
    assert [c.outcome for c in recorder.calls] == ["ok"]


def test_a_rejected_answer_is_sent_back_with_the_complaints(pack: EvidencePack) -> None:
    """The retry carries what was wrong. "You wrote X, the fact says Y" beats "try again"."""
    item = pack.items[0]
    wrong = _one_item(pack, why="It was 3.2%.",
                      claims=[Claim(text="It was 3.2%.", metric_ref=item.value_ref)])
    client, backend, recorder = _client(_as_json(wrong), _as_json(_good(pack)))

    result = narrate(pack, client)
    assert result.source == "llm_retry" and result.attempts == 2 and result.validation.ok
    second_prompt = backend.calls[1][1]
    assert "rejected" in second_prompt and "3.2%" in second_prompt
    assert [c.purpose for c in recorder.calls] == ["narrate", "narrate_retry"]


def test_two_bad_answers_fall_back_to_the_templated_brief(pack: EvidencePack) -> None:
    wrong = _as_json(_one_item(pack, id="I99"))
    client, backend, _ = _client(wrong, wrong)

    result = narrate(pack, client)
    assert result.source == "fallback" and result.attempts == 2
    assert result.validation.ok                       # the brief that ships is still checked
    assert result.fallback_reason and "validator rejected" in result.fallback_reason
    assert len(backend.calls) == 2                    # and no third attempt
    assert result.brief.items and result.brief.summary


def test_invalid_json_falls_back_rather_than_failing_the_morning(pack: EvidencePack) -> None:
    client, _backend, recorder = _client("not json at all", "still not json")
    result = narrate(pack, client)
    assert result.source == "fallback" and result.used_fallback
    assert "model failed twice" in (result.fallback_reason or "")
    assert [c.outcome for c in recorder.calls] == ["invalid_output", "invalid_output"]


def test_a_provider_outage_falls_back(pack: EvidencePack) -> None:
    client, _backend, recorder = _client(LLMError("503: service unavailable"),
                                         LLMError("503: service unavailable"))
    result = narrate(pack, client)
    assert result.source == "fallback"
    assert "503" in (result.fallback_reason or "")
    assert [c.outcome for c in recorder.calls] == ["error", "error"]


def test_an_outage_on_the_first_try_still_gets_a_second(pack: EvidencePack) -> None:
    client, backend, _ = _client(LLMError("500: transient"), _as_json(_good(pack)))
    result = narrate(pack, client)
    assert result.source == "llm_retry" and len(backend.calls) == 2


def test_no_model_configured_is_a_fallback_not_an_error(pack: EvidencePack) -> None:
    result = narrate(pack, None)
    assert result.source == "fallback" and result.attempts == 0
    assert "LLM_PROVIDER=none" in (result.fallback_reason or "")
    assert result.validation.ok


def test_the_prompt_carries_the_pack_and_nothing_else(pack: EvidencePack) -> None:
    client, backend, _ = _client(_as_json(_good(pack)))
    narrate(pack, client)
    _system, user = backend.calls[0]
    payload = json.loads(user[user.index("{"):])
    assert set(payload) == set(pack.for_prompt())
    assert "PLT-01" not in json.dumps(payload) or any(
        i.segment.get("plant") == "PLT-01" for i in pack.items)


def test_the_result_reports_what_happened_for_the_ops_record(pack: EvidencePack) -> None:
    client, _backend, _recorder = _client(_as_json(_good(pack)))
    result = narrate(pack, client).to_dict()
    assert result["source"] == "llm" and result["citation_coverage"] == 1.0
    assert result["complaints"] == [] and result["claims"] > 0
    assert json.dumps(result)


def test_an_invalid_output_is_recorded_before_it_is_retried(pack: EvidencePack) -> None:
    """A call that produced rubbish still cost money and still happened; the record says so."""
    client, _backend, recorder = _client("{}", _as_json(_good(pack)))
    result = narrate(pack, client)
    assert result.source == "llm_retry"
    assert recorder.calls[0].outcome == "invalid_output"
    assert recorder.calls[0].input_tokens == 100 and recorder.calls[0].latency_ms >= 0
    with pytest.raises(InvalidOutput):
        raise InvalidOutput("shape check")


# --- what the narration sample taught the validator --------------------------------------------


def test_a_typographic_hyphen_is_still_a_plant_name(pack: EvidencePack) -> None:
    """The model writes PLT‑02 with a non-breaking hyphen; a validator comparing bytes sees a bare "02".

    Three of the four fallbacks in the second narration sample were this, and none of them was a writing
    mistake. Normalising first is the difference between measuring the model and measuring its typography.
    """
    item = pack.items[0]
    fancy = item.segment_label.replace("-", "‑")
    brief = _one_item(pack, why=f"{fancy} was affected.",
                      claims=[Claim(text=f"{fancy} was {pack.facts[item.value_ref].display}.",
                                    metric_ref=item.value_ref)])
    assert validate(brief, pack).ok


def test_a_change_may_be_written_without_its_minus_sign(pack: EvidencePack) -> None:
    """"fell by 9.4 points" is how a person writes a change of -9.4; the direction is in the verb."""
    item = next(i for i in pack.items if i.delta_ref and pack.facts[i.delta_ref].value < 0)
    delta_ref = item.delta_ref
    assert delta_ref is not None
    unsigned = pack.facts[delta_ref].display.lstrip("-")
    brief = _one_item(pack, id=item.id, severity=item.severity, why=f"It fell by {unsigned}.",
                      claims=[Claim(text=f"It fell by {unsigned}.", metric_ref=delta_ref)])
    assert validate(brief, pack).ok


def test_masking_a_name_does_not_hide_a_wrong_number_beside_it(pack: EvidencePack) -> None:
    """The masking must not become a place to smuggle a claim through."""
    item = pack.items[0]
    brief = _one_item(pack, why=f"{item.segment_label} was 12.3%.",
                      claims=[Claim(text=f"{item.segment_label} was 12.3%.", metric_ref=item.value_ref)])
    result = validate(brief, pack)
    assert not result.ok and any(c.rule == "number-not-in-pack" for c in result.complaints)


def test_the_schema_has_no_union_types(pack: EvidencePack) -> None:
    """Strict mode on some providers rejects a nullable field, and the brief falls back for nothing."""
    def walk(node: dict) -> None:
        assert isinstance(node.get("type"), str), f"union type in {node}"
        for child in node.get("properties", {}).values():
            walk(child)
        if "items" in node:
            walk(node["items"])

    walk(brief_schema())
