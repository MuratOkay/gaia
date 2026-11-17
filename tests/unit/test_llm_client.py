"""LLM client request shaping and response parsing.

Covers the provider quirks found in practice and documented by DeepSeek:
thinking toggle, omission of temperature in thinking mode, and both
``reasoning_content`` (DeepSeek) and ``reasoning`` (OpenAI-compatible gateway).
"""

from __future__ import annotations

import json

import httpx
import pytest

from gaia.core.config import LLMConfig
from gaia.llm.base import LLMError, Message
from gaia.llm.deepseek import DeepSeekClient


def _config(**overrides) -> LLMConfig:
    base = {
        "provider": "deepseek",
        "base_url": "https://llm.example/v1",
        "model": "deepseek-v4.1-flash",
        "api_key": "test-key",
        "max_output_tokens": 1000,
    }
    base.update(overrides)
    return LLMConfig(**base)


def _payload(message: dict, *, model: str = "deepseek-v4.1-flash") -> dict:
    return {
        "id": "req-1",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 7},
        },
    }


def _client_returning(payload: dict) -> DeepSeekClient:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = DeepSeekClient(_config(), client=http)
    client.captured = captured  # type: ignore[attr-defined]
    return client


def test_thinking_disabled_sends_toggle_and_temperature():
    client = _client_returning(_payload({"content": "ok"}))
    client.complete([Message("user", "hi")], thinking=False, temperature=0.0)

    body = client.captured["body"]
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.0
    assert body["model"] == "deepseek-v4.1-flash"
    assert client.captured["auth"] == "Bearer test-key"


def test_thinking_enabled_omits_temperature():
    client = _client_returning(_payload({"content": "ok"}))
    client.complete([Message("user", "hi")], thinking=True, temperature=0.7)

    body = client.captured["body"]
    assert body["thinking"] == {"type": "enabled"}
    # Thinking mode ignores temperature; sending it would be a silent no-op.
    assert "temperature" not in body


def test_json_mode_sets_response_format():
    client = _client_returning(_payload({"content": '{"a":1}'}))
    client.complete([Message("user", "hi")], json_mode=True, thinking=False)

    assert client.captured["body"]["response_format"] == {"type": "json_object"}


def test_json_mode_absent_by_default():
    client = _client_returning(_payload({"content": "ok"}))
    client.complete([Message("user", "hi")])

    assert "response_format" not in client.captured["body"]


def test_parses_reasoning_content_field():
    client = _client_returning(_payload({"content": "a", "reasoning_content": "cot"}))
    result = client.complete([Message("user", "hi")], thinking=True)

    assert result.reasoning == "cot"


def test_parses_gateway_reasoning_field():
    client = _client_returning(_payload({"content": "a", "reasoning": "gateway cot"}))
    result = client.complete([Message("user", "hi")], thinking=True)

    assert result.reasoning == "gateway cot"


def test_usage_and_cache_fields():
    client = _client_returning(_payload({"content": "a"}))
    result = client.complete([Message("user", "hi")])

    assert result.usage.cached_tokens == 5
    assert result.usage.reasoning_tokens == 7
    assert result.usage.cache_hit is True


def test_missing_api_key_raises():
    client = DeepSeekClient(_config(api_key=""), client=httpx.Client())
    with pytest.raises(LLMError, match="API_KEY"):
        client.complete([Message("user", "hi")])


def test_blank_content_is_retried_then_fails():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_payload({"content": "   "}))

    client = DeepSeekClient(
        _config(retry={"max_attempts": 2, "backoff_base_s": 0.0, "retry_on_blank_content": True}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LLMError):
        client.complete([Message("user", "hi")], json_mode=True)
    assert calls["n"] == 2


def test_client_error_raises_with_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    client = DeepSeekClient(_config(), client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(LLMError) as excinfo:
        client.complete([Message("user", "hi")])
    assert excinfo.value.status == 401


def test_a_refusal_says_why():
    """T4 (2026-11-17): a 400 is not retried, so the gateway's reason travels in the error
    message — OpenAI-style ``error.message``, or the body's start — on one short line."""
    bodies = [
        (400, {"error": {"message": "max_tokens too large\nfor this mode", "code": "x"}},
         "http 400: max_tokens too large for this mode"),
        (400, {"detail": "model not found"}, "http 400: model not found"),
        (422, None, "http 422: plain " + "text " * 60),
    ]
    for status, payload, expected in bodies:
        def handler(request: httpx.Request, status=status, payload=payload) -> httpx.Response:
            if payload is None:
                return httpx.Response(status, text="plain " + "text " * 60)
            return httpx.Response(status, json=payload)

        client = DeepSeekClient(_config(),
                                client=httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(LLMError) as excinfo:
            client.complete([Message("user", "hi")])
        message = str(excinfo.value)
        assert excinfo.value.status == status
        assert message == expected[: len(message)] and len(message) <= len("http 422: ") + 200
        assert message.startswith(expected[:30])


def test_an_evaluation_keeps_the_gateway_answer_of_a_refused_call_only():
    from gaia.evaluation.runner import _gateway_answer
    from gaia.llm.structured import CallRecord

    base = {"purpose": "semantics", "model": "m", "prompt_version": "v", "prompt_hash": "h",
            "thinking": False, "json_mode": True, "attempt": 1}
    refused = CallRecord(**base, error="http 400: too long", content='{"error": "too long"}')
    invalid = CallRecord(**base, error="validation", content='{"columns": []}',
                         finish_reason="stop")
    fine = CallRecord(**base, content="{}", finish_reason="stop", parsed_ok=True)
    assert _gateway_answer(refused) == {"gateway_response": '{"error": "too long"}'}
    assert _gateway_answer(invalid) == {}  # a model answer: never copied into results
    assert _gateway_answer(fine) == {}


def test_read_timeout_uses_its_own_smaller_budget():
    """A timeout already burned llm.timeout_s; do not retry it 3 times."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("too slow", request=request)

    client = DeepSeekClient(
        _config(
            retry={
                "max_attempts": 3,
                "max_attempts_rate_limit": 8,
                "max_attempts_timeout": 2,
                "backoff_base_s": 0.0,
                "max_backoff_s": 0.0,
                "retry_on_blank_content": True,
            }
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LLMError) as excinfo:
        client.complete([Message("user", "hi")])

    assert calls["n"] == 2
    assert "timed out" in str(excinfo.value)
    assert "llm.timeout_s" in str(excinfo.value)


def test_timeout_message_is_distinct_from_gateway_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = DeepSeekClient(
        _config(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LLMError) as excinfo:
        client.complete([Message("user", "hi")])

    # Not a concurrency/5xx message: the operator needs to tell these apart.
    assert "gateway capacity" not in str(excinfo.value)
    assert excinfo.value.status is None
