"""Every completion of an evaluation run, kept on disk.

An evaluation makes a few hundred slow calls (PHASES, "Estimate"). The recorder
stores each answer under the request's key (``gaia.llm.replay.request_key``), so

* an interrupted run resumes where it stopped — answered requests cost nothing;
* judging, scoring and the report can be recomputed offline, and anyone can see
  exactly what the model was asked and what it said;
* usage (tokens, latency) is kept per call for the cost part of the report.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from gaia.llm.base import LLMClient, LLMResponse, Message, Usage
from gaia.llm.replay import request_key

__all__ = ["RecordingLLMClient"]


class RecordingLLMClient:
    def __init__(self, inner: LLMClient | None, directory: str | Path) -> None:
        self.inner = inner
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.model = getattr(inner, "model", "recorded")
        self.live_calls = 0
        self.replayed = 0
        self._lock = threading.Lock()

    def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        thinking: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        params = {"json_mode": json_mode, "thinking": thinking, "max_tokens": max_tokens,
                  "temperature": None if thinking else temperature}
        key = request_key(messages, params)
        path = self.dir / f"{key}.json"
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            with self._lock:
                self.replayed += 1
            return LLMResponse(
                content=doc["content"],
                model=doc.get("model", self.model),
                reasoning=doc.get("reasoning"),
                finish_reason=doc.get("finish_reason", "stop"),
                usage=Usage(**doc.get("usage", {})),
                latency_ms=0,  # not a new call: the recorded latency is in the file
                request_id=key,
            )
        if self.inner is None:
            raise RuntimeError(f"no recorded answer for {key} and no model to ask")
        response = self.inner.complete(
            messages, json_mode=json_mode, thinking=thinking, max_tokens=max_tokens,
            temperature=temperature,
        )
        with self._lock:
            self.live_calls += 1
        if response.finish_reason != "length" and response.content.strip():
            doc = {
                "key": key,
                "model": response.model,
                "content": response.content,
                "reasoning": response.reasoning,
                "finish_reason": response.finish_reason,
                "usage": response.usage.__dict__,
                "latency_ms": response.latency_ms,
                "purpose": _purpose(messages),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        return response


def _purpose(messages: list[Message]) -> str:
    system = messages[0].content if messages else ""
    if system.startswith("You are a sceptical reviewer"):
        return "critique"
    if system.startswith("You are judging"):
        return "judge"
    return "semantics"
