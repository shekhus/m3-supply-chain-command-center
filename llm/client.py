"""The only way this repo calls a model (CLAUDE.md coding conventions).

Copied from m3-trusted-data-foundation, where it has been carrying real traffic: one provider at a time
behind `Backend` (anthropic or groq, chosen by LLM_PROVIDER), a single request with a JSON schema as the
output format, parsed by the caller's contract, and every attempt recorded to `ops.llm_calls` with model,
prompt hash, tokens, latency, cost and outcome.

**It never retries an answer on its own.** That decision belongs to the caller, because here the caller is
`narrate/`, and its retry policy is part of the product: one regeneration carrying the validator's complaint,
then a templated brief with no model in it at all (principle 7 — the brief always ships). A client that
quietly retried would hide the fallback rate, which is one of the numbers this project reports honestly.

The Groq backend waits out tokens-per-minute rate limits within a budget. That is transport, not a second
answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, TypeVar

import httpx
from sqlalchemy import Engine, text

from app.config import Settings

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
Outcome = Literal["ok", "invalid_output", "fallback", "error"]
T = TypeVar("T")


class LLMError(RuntimeError):
    """The provider failed or declined; no usable text came back."""


class InvalidOutput(ValueError):
    """Text came back but did not satisfy the caller's contract."""


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str


class Backend(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str, schema: dict, max_tokens: int) -> Completion: ...


@dataclass(frozen=True)
class CallRecord:
    purpose: str
    provider: str
    model: str
    prompt_hash: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    cost_usd: float | None
    outcome: Outcome
    error: str | None
    batch_id: str | None = None


class Recorder(Protocol):
    def record(self, call: CallRecord) -> None: ...


@dataclass
class MemoryRecorder:
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, call: CallRecord) -> None:
        self.calls.append(call)


@dataclass(frozen=True)
class DbRecorder:
    engine: Engine

    def record(self, call: CallRecord) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO ops.llm_calls (batch_id, purpose, provider, model, prompt_hash, input_tokens, "
                "output_tokens, latency_ms, cost_usd, outcome, error) "
                "VALUES (:batch_id, :purpose, :provider, :model, :prompt_hash, :input_tokens, "
                ":output_tokens, :latency_ms, :cost_usd, :outcome, :error)"),
                call.__dict__)


class AnthropicBackend:
    provider = "anthropic"

    def __init__(self, model: str) -> None:
        import anthropic  # imported lazily so LLM_PROVIDER=none never needs the SDK configured

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()  # credentials from ANTHROPIC_API_KEY (loaded from .env)
        self.model = model

    def complete(self, system: str, user: str, schema: dict, max_tokens: int) -> Completion:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except self._anthropic.APIStatusError as exc:
            raise LLMError(f"{exc.status_code}: {exc.message} (request {exc.request_id})") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError(f"connection error: {exc}") from exc
        if response.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"stop_reason={response.stop_reason} (request {response._request_id})")
        body = next((b.text for b in response.content if b.type == "text"), "")
        return Completion(body, response.usage.input_tokens, response.usage.output_tokens,
                          str(response.stop_reason))


MAX_RATE_LIMIT_WAIT_S = 65.0  # one wait: a tokens-per-minute window; a longer retry-after (daily quota) fails
MAX_TOTAL_RATE_LIMIT_WAIT_S = 240.0  # all waits for one request


def _retry_after_seconds(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"]) + 0.5
    except (KeyError, ValueError):
        return None


class GroqBackend:
    """Groq's OpenAI-compatible chat completions with strict JSON-schema output (constrained decoding).

    Called with httpx (already a dependency) rather than an extra SDK. Strict mode guarantees the JSON matches
    the schema; the caller's contract still checks what a schema cannot (every header column once, no
    duplicate targets), so invalid answers remain possible and are handled by the caller.
    """

    provider = "groq"
    url = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, model: str, api_key: str, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is not set")
        self.model = model
        self._key = api_key
        self._http = http or httpx.Client(timeout=120.0)
        self._sleep = sleep

    def _post(self, body: dict) -> httpx.Response:
        try:
            return self._http.post(self.url, json=body, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as exc:
            raise LLMError(f"connection error: {exc}") from exc

    def complete(self, system: str, user: str, schema: dict, max_tokens: int) -> Completion:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "strict": True, "schema": schema}},
        }
        response = self._post(body)
        waited = 0.0
        while response.status_code == 429:  # tokens-per-minute limit: wait it out within a budget (transport)
            wait = _retry_after_seconds(response)
            if wait is None or wait > MAX_RATE_LIMIT_WAIT_S or waited + wait > MAX_TOTAL_RATE_LIMIT_WAIT_S:
                break
            self._sleep(wait)
            waited += wait
            response = self._post(body)
        request_id = response.headers.get("x-request-id", "")
        if not response.is_success:
            try:
                message = response.json().get("error", {}).get("message", response.text)
            except ValueError:
                message = response.text
            raise LLMError(f"{response.status_code}: {message[:500]} (request {request_id})")
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise LLMError(f"finish_reason={choice.get('finish_reason')} (request {request_id})")
        usage = data.get("usage", {})
        return Completion(choice["message"].get("content") or "", int(usage.get("prompt_tokens", 0)),
                          int(usage.get("completion_tokens", 0)), str(choice["finish_reason"]))


class LLMClient:
    def __init__(self, backend: Backend, recorder: Recorder, price_in_per_mtok: float,
                 price_out_per_mtok: float) -> None:
        self.backend = backend
        self.recorder = recorder
        self.price_in = price_in_per_mtok
        self.price_out = price_out_per_mtok

    def complete_json(self, purpose: str, system: str, user: str, schema: dict, parse: Callable[[dict], T],
                      max_tokens: int = 16000, batch_id: str | None = None) -> T:
        fingerprint = json.dumps([self.backend.model, system, user, schema], sort_keys=True)
        prompt_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        started = time.perf_counter()

        def record(outcome: Outcome, completion: Completion | None, error: str | None) -> None:
            tokens_in = completion.input_tokens if completion else None
            tokens_out = completion.output_tokens if completion else None
            cost = (round(tokens_in * self.price_in / 1e6 + tokens_out * self.price_out / 1e6, 6)
                    if tokens_in is not None and tokens_out is not None else None)
            self.recorder.record(CallRecord(
                purpose=purpose, provider=self.backend.provider, model=self.backend.model,
                prompt_hash=prompt_hash, input_tokens=tokens_in, output_tokens=tokens_out,
                latency_ms=int((time.perf_counter() - started) * 1000), cost_usd=cost, outcome=outcome,
                error=error, batch_id=batch_id))

        try:
            completion = self.backend.complete(system, user, schema, max_tokens)
        except LLMError as exc:
            record("error", None, str(exc)[:2000])
            raise
        try:
            result = parse(json.loads(completion.text))
        except (ValueError, TypeError) as exc:  # JSONDecodeError and pydantic ValidationError are ValueErrors
            record("invalid_output", completion, str(exc)[:2000])
            raise InvalidOutput(str(exc)) from exc
        record("ok", completion, None)
        return result


def build_client(settings: Settings, recorder: Recorder) -> LLMClient | None:
    """None when LLM_PROVIDER=none: callers then use the heuristic only."""
    backend: Backend
    if settings.llm_provider == "none":
        return None
    if settings.llm_provider == "anthropic":
        backend = AnthropicBackend(settings.llm_model)
    elif settings.llm_provider == "groq":
        backend = GroqBackend(settings.llm_model, os.environ.get("GROQ_API_KEY", ""))
    else:
        raise ValueError(f"unsupported LLM_PROVIDER '{settings.llm_provider}' "
                         "(supported: anthropic, groq, none)")
    return LLMClient(backend, recorder, settings.llm_price_in, settings.llm_price_out)


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")
