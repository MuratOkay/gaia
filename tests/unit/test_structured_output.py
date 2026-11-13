"""Structured completions: validation, bounded retry with feedback, caching,
and recording of every call including failures."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from gaia.llm.base import LLMError, LLMResponse, Message, Usage
from gaia.llm.cache import MemoryCache, NullCache, RedisCache, llm_cache_key
from gaia.llm.structured import StructuredOutputError, complete_structured, extract_json


class Thing(BaseModel):
    name: str
    size: int


class ScriptedLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.received: list[list[Message]] = []

    def complete(self, messages, **kwargs):
        self.received.append(list(messages))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        content, finish = item if isinstance(item, tuple) else (item, "stop")
        return LLMResponse(
            content=content,
            model="m",
            finish_reason=finish,
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )


MESSAGES = [Message("system", "sys"), Message("user", "give me json")]


def _run(llm, **kw):
    defaults = {
        "purpose": "semantics",
        "model": "m",
        "prompt_version": "p.v1+abc",
        "thinking": False,
        "validation_retries": 1,
    }
    defaults.update(kw)
    return complete_structured(llm, MESSAGES, Thing, **defaults)


def test_valid_first_try():
    llm = ScriptedLLM('{"name": "a", "size": 1}')
    result = _run(llm)
    assert result.value == Thing(name="a", size=1)
    assert len(result.calls) == 1
    assert result.calls[0].parsed_ok
    assert result.calls[0].usage.prompt_tokens == 10


def test_invalid_then_valid_feeds_errors_back_and_keeps_prefix():
    llm = ScriptedLLM('{"name": "a"}', '{"name": "a", "size": 2}')
    result = _run(llm)

    assert result.value.size == 2
    assert [c.parsed_ok for c in result.calls] == [False, True]
    assert "size" in result.calls[0].error
    retry = llm.received[1]
    assert retry[:2] == MESSAGES  # stable prefix for the provider cache
    assert retry[2].role == "assistant" and retry[2].content == '{"name": "a"}'
    assert retry[3].role == "user" and "size" in retry[3].content


def test_gives_up_after_retries_and_keeps_raw_payloads():
    llm = ScriptedLLM("not json", '{"name": 1}')
    with pytest.raises(StructuredOutputError) as excinfo:
        _run(llm)
    calls = excinfo.value.calls
    assert [c.content for c in calls] == ["not json", '{"name": 1}']
    assert all(not c.parsed_ok and c.error for c in calls)


def test_truncated_output_fails_without_retry():
    llm = ScriptedLLM(('{"name": "a", "si', "length"))
    with pytest.raises(StructuredOutputError, match="max_output_tokens"):
        _run(llm, max_tokens=100)
    assert len(llm.received) == 1


def test_transport_failure_is_recorded():
    llm = ScriptedLLM(LLMError("http 500", status=500, body="boom"))
    with pytest.raises(StructuredOutputError) as excinfo:
        _run(llm)
    (call,) = excinfo.value.calls
    assert "http 500" in call.error
    assert call.content == "boom"


def test_checker_problems_trigger_retry():
    llm = ScriptedLLM('{"name": "a", "size": 1}', '{"name": "b", "size": 1}')
    result = _run(llm, checker=lambda v: [] if v.name == "b" else ["name must be b"])
    assert result.value.name == "b"
    assert "name must be b" in llm.received[1][-1].content


def test_cache_hit_skips_the_call_and_is_recorded():
    cache = MemoryCache()
    first = _run(ScriptedLLM('{"name": "a", "size": 1}'), cache=cache)
    assert not first.cache_hit

    llm = ScriptedLLM()  # would raise IndexError if called
    second = _run(llm, cache=cache)
    assert second.cache_hit
    assert second.value == first.value
    assert llm.received == []


def test_invalid_cached_payload_is_ignored():
    cache = MemoryCache()
    key = llm_cache_key(
        model="m",
        prompt_version="p.v1+abc",
        messages=[m.as_dict() for m in MESSAGES],
        params={
            "json_mode": True,
            "thinking": False,
            "max_tokens": None,
            "temperature": None,
            "schema": "Thing",
        },
    )
    cache.data[key] = '{"stale": true}'
    result = _run(ScriptedLLM('{"name": "a", "size": 1}'), cache=cache)
    assert not result.cache_hit
    assert result.value.name == "a"


def test_failed_results_are_not_cached():
    cache = MemoryCache()
    with pytest.raises(StructuredOutputError):
        _run(ScriptedLLM("x", "y"), cache=cache)
    assert cache.data == {}


def test_cache_key_depends_on_prompt_version_and_thinking():
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    a = llm_cache_key(prompt_version="v1", params={"thinking": True}, **base)
    b = llm_cache_key(prompt_version="v2", params={"thinking": True}, **base)
    c = llm_cache_key(prompt_version="v1", params={"thinking": False}, **base)
    assert len({a, b, c}) == 3
    assert a == llm_cache_key(prompt_version="v1", params={"thinking": True}, **base)
    assert a.startswith("llm:m:v1:")


def test_extract_json_tolerates_code_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json(' {"a": 1} ') == {"a": 1}
    with pytest.raises(json.JSONDecodeError):
        extract_json("nope")


def test_redis_cache_degrades_to_miss_on_error():
    class Broken:
        def mget(self, keys):
            raise ConnectionError("down")

        def pipeline(self, transaction=False):
            raise ConnectionError("down")

    cache = RedisCache("redis://unused", client=Broken())
    assert cache.get_many(["a", "b"]) == [None, None]
    cache.set_many({"a": "1"}, 60)  # must not raise
    assert cache.get_many(["a"]) == [None]
    assert NullCache().get_many(["a"]) == [None]
