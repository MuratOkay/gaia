"""Structured (JSON) completions validated against Pydantic models.

JSON mode guarantees syntactically valid JSON, not schema conformance
(OUTLINE §3). This module closes that gap:

1. Look up the response cache (keyed by model, prompt version, messages and
   parameters). A cached payload is re-validated before use, so a schema
   change can never serve a stale shape.
2. Call the model in JSON mode. Transport errors, 429/5xx and blank content
   are already retried inside the provider client.
3. Parse and validate with Pydantic, then run the caller's semantic checks
   (e.g. "every column of the bundle is answered exactly once").
4. On failure, append the invalid answer and the exact errors to the
   conversation and ask again, at most ``validation_retries`` times. The
   original messages stay unchanged at the front, so the provider's prefix
   cache still applies.
5. Every HTTP-level call, successful or not, is returned as a ``CallRecord``
   with its raw payload, so failures are recorded rather than swallowed
   (PHASES, cross-cutting "Failure").

Truncated output (``finish_reason == "length"``) fails immediately: re-asking
would truncate again at the same limit. The error says so (``truncated``) and
where the tokens went: with thinking on, ``max_tokens`` covers the chain of
thought *and* the answer, so a narrow table can truncate when the model thinks
long (3C, 2026-11-16: 12,437 of 16,000 tokens were thinking on a 14-column
table). The caller decides what to do — the reasoner re-asks in smaller column
chunks (``semantics.reasoner``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from gaia.llm.base import LLMClient, LLMError, Message, Usage
from gaia.llm.cache import KVCache, NullCache, llm_cache_key

__all__ = [
    "CallRecord",
    "StructuredOutputError",
    "StructuredResult",
    "complete_structured",
    "extract_json",
    "truncation_message",
]

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass
class CallRecord:
    """One model call (or cache hit), destined for ``audit.llm_call``."""

    purpose: str
    model: str
    prompt_version: str
    prompt_hash: str
    thinking: bool
    json_mode: bool
    attempt: int  # 1 = first try; >1 = validation retry
    cache_hit: bool = False
    content: str | None = None
    reasoning: str | None = None
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    transport_attempts: int = 0
    request_id: str | None = None
    parsed_ok: bool = False
    error: str | None = None
    # The model the provider says answered (``response.model``), which can differ from the
    # configured id (e.g. a gateway's ``deepseek/…`` prefix, or a provider switch during a
    # long evaluation, 2026-11-17 triage T3). None for a cache hit or a failed call.
    answered_by: str | None = None


@dataclass
class StructuredResult[T: BaseModel]:
    value: T
    calls: list[CallRecord]

    @property
    def cache_hit(self) -> bool:
        return any(c.cache_hit for c in self.calls)


class StructuredOutputError(LLMError):
    """No schema-valid answer was obtained. ``calls`` keeps every raw payload.

    ``truncated``: the answer hit ``max_tokens`` (a smaller request may fit)."""

    def __init__(self, message: str, *, calls: list[CallRecord], truncated: bool = False):
        super().__init__(message)
        self.calls = calls
        self.truncated = truncated


def truncation_message(max_tokens: int | None, usage: Usage) -> str:
    """``output truncated at max_tokens=N: thinking T, answer A token(s)``."""
    thinking = usage.reasoning_tokens
    answer = max(0, usage.completion_tokens - thinking)
    return (
        f"output truncated at max_tokens={max_tokens}: thinking {thinking}, "
        f"answer {answer} token(s)"
    )


def extract_json(content: str) -> object:
    """Parse model output as JSON, tolerating a Markdown code fence."""
    text = content.strip()
    match = _FENCE.match(text)
    if match:
        text = match.group(1)
    return json.loads(text)


def _validate[T: BaseModel](
    content: str,
    schema: type[T],
    checker: Callable[[T], list[str]] | None,
) -> tuple[T | None, list[str]]:
    try:
        data = extract_json(content)
    except json.JSONDecodeError as exc:
        return None, [f"response is not valid JSON: {exc}"]
    try:
        value = schema.model_validate(data)
    except ValidationError as exc:
        errors = []
        for err in exc.errors()[:25]:
            loc = ".".join(str(p) for p in err.get("loc", ()))
            errors.append(f"{loc or '<root>'}: {err.get('msg')}")
        return None, errors
    if checker is not None:
        problems = checker(value)
        if problems:
            return None, problems[:25]
    return value, []


def _feedback(errors: list[str]) -> str:
    bullet = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous response did not pass validation:\n"
        f"{bullet}\n\n"
        "Return the complete corrected json object only, following the required "
        "shape exactly. Do not omit any item."
    )


def complete_structured[T: BaseModel](
    client: LLMClient,
    messages: list[Message],
    schema: type[T],
    *,
    purpose: str,
    model: str,
    prompt_version: str,
    thinking: bool,
    max_tokens: int | None = None,
    temperature: float | None = None,
    validation_retries: int = 1,
    checker: Callable[[T], list[str]] | None = None,
    cache: KVCache | None = None,
    cache_ttl_s: int = 2_592_000,
) -> StructuredResult[T]:
    cache = cache or NullCache()
    params = {
        "json_mode": True,
        "thinking": thinking,
        "max_tokens": max_tokens,
        # Temperature is ignored in thinking mode; keep it out of the key then.
        "temperature": None if thinking else temperature,
        "schema": schema.__name__,
    }
    key = llm_cache_key(
        model=model,
        prompt_version=prompt_version,
        messages=[m.as_dict() for m in messages],
        params=params,
    )
    prompt_hash = key.rsplit(":", 1)[-1]
    calls: list[CallRecord] = []

    def record(attempt: int, **kwargs) -> CallRecord:
        rec = CallRecord(
            purpose=purpose,
            model=model,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            thinking=thinking,
            json_mode=True,
            attempt=attempt,
            **kwargs,
        )
        calls.append(rec)
        return rec

    cached = cache.get_many([key])[0]
    if cached is not None:
        value, errors = _validate(cached, schema, checker)
        if value is not None:
            record(1, cache_hit=True, content=cached, parsed_ok=True)
            return StructuredResult(value=value, calls=calls)
        # A cached payload that no longer validates is ignored, not trusted.

    conversation = list(messages)
    max_attempts = 1 + max(0, validation_retries)

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.complete(
                conversation,
                json_mode=True,
                thinking=thinking,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except LLMError as exc:
            record(attempt, error=f"{exc}", content=getattr(exc, "body", None))
            raise StructuredOutputError(
                f"{purpose}: completion failed: {exc}", calls=calls
            ) from exc

        rec = record(
            attempt,
            content=response.content,
            reasoning=response.reasoning,
            finish_reason=response.finish_reason,
            usage=response.usage,
            latency_ms=response.latency_ms,
            transport_attempts=response.attempts,
            request_id=response.request_id,
            answered_by=response.model,
        )

        if response.finish_reason == "length":
            rec.error = truncation_message(max_tokens, response.usage)
            raise StructuredOutputError(f"{purpose}: {rec.error}", calls=calls, truncated=True)

        value, errors = _validate(response.content, schema, checker)
        if value is not None:
            rec.parsed_ok = True
            cache.set_many({key: response.content}, cache_ttl_s)
            return StructuredResult(value=value, calls=calls)

        rec.error = "; ".join(errors)
        conversation = [
            *conversation,
            Message("assistant", response.content),
            Message("user", _feedback(errors)),
        ]

    raise StructuredOutputError(
        f"{purpose}: no schema-valid response after {max_attempts} attempt(s): {calls[-1].error}",
        calls=calls,
    )
