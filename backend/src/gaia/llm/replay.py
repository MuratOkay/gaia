"""File-based LLM provider: answers come from files, requests go to files.

    llm.provider: replay            (or: make reason ARGS="--replay DIR")
    llm.replay_dir: /path/to/dir

For every completion the client computes a key from the exact messages and
parameters. If ``DIR/responses/<key>.json`` exists, its content is the model's
answer. Otherwise the request is written to ``DIR/requests/<key>.json`` (plus a
readable ``.md`` twin) and the call fails with "awaiting response" — recorded
like any failure — so the next run, after someone or something has written the
answer, picks it up.

Uses:

* deterministic end-to-end tests without a network;
* a person, or another system, acting as the model — e.g. reviewing prompts
  before they go to a paid provider, or evaluating a model that has no API.

Answers produced this way are **not** measurements of the configured model:
``model`` in every record says ``replay``.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from gaia.llm.base import LLMError, LLMResponse, Message, Usage

__all__ = ["ReplayLLMClient", "pending_requests", "request_key"]


def request_key(messages: list[Message], params: dict) -> str:
    material = json.dumps(
        {"messages": [m.as_dict() for m in messages], "params": params},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _describe(messages: list[Message]) -> dict[str, str | None]:
    system = messages[0].content if messages else ""
    purpose = "critique" if system.startswith("You are a sceptical reviewer") else "semantics"
    table = None
    for m in messages:
        marker = '"table": "'
        at = m.content.find(marker)
        if m.role == "user" and at >= 0:
            table = m.content[at + len(marker):].split('"', 1)[0]
            break
    return {"purpose": purpose, "table": table}


class ReplayLLMClient:
    model = "replay"

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        (self.dir / "requests").mkdir(parents=True, exist_ok=True)
        (self.dir / "responses").mkdir(parents=True, exist_ok=True)

    def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        thinking: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        params = {"json_mode": json_mode, "thinking": thinking, "max_tokens": max_tokens}
        key = request_key(messages, params)
        answer = self.dir / "responses" / f"{key}.json"
        if answer.exists():
            started = time.perf_counter()
            content = answer.read_text(encoding="utf-8")
            return LLMResponse(
                content=content,
                model=self.model,
                finish_reason="stop",
                usage=Usage(),
                latency_ms=int((time.perf_counter() - started) * 1000),
                request_id=key,
            )
        meta = _describe(messages)
        request = {
            "key": key,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **meta,
            "params": params,
            "messages": [m.as_dict() for m in messages],
            "answer_to": f"responses/{key}.json",
        }
        (self.dir / "requests" / f"{key}.json").write_text(
            json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        readable = "\n\n".join(f"## {m.role}\n\n{m.content}" for m in messages)
        (self.dir / "requests" / f"{key}.md").write_text(
            f"# {meta['purpose']} — {meta['table']}\n\nAnswer in `responses/{key}.json` "
            f"with the JSON only.\n\n{readable}\n",
            encoding="utf-8",
        )
        raise LLMError(f"awaiting replay response {key} ({meta['purpose']} {meta['table']})")


def pending_requests(directory: str | Path) -> list[dict]:
    """Requests without an answer yet, oldest first."""
    base = Path(directory)
    out = []
    for path in sorted((base / "requests").glob("*.json")):
        if not (base / "responses" / path.name).exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            out.append({k: doc[k] for k in ("key", "purpose", "table", "created_at")})
    return sorted(out, key=lambda d: d["created_at"])
