"""Provider-neutral LLM interface.

Only two things vary between providers: how a request is shaped and how the
response is unpacked. Everything above this layer (reasoning, critique, SQL
generation) programs against these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cache_created_tokens: int = 0

    @property
    def cache_hit(self) -> bool:
        return self.cached_tokens > 0


@dataclass
class LLMResponse:
    """A single completion.

    ``reasoning`` holds the model's chain of thought when thinking mode is on.
    It is kept separate from ``content`` so that scoring can inspect reasoning
    without leaking it into answers shown to users.
    """

    content: str
    model: str
    reasoning: str | None = None
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    attempts: int = 1
    request_id: str | None = None

    @property
    def is_blank(self) -> bool:
        """JSON mode can return whitespace-only content even with finish=stop."""
        return not self.content.strip()


@runtime_checkable
class LLMClient(Protocol):
    """Minimal contract used by the semantic and query pipelines."""

    def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        thinking: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        ...


class LLMError(RuntimeError):
    """Raised when a completion cannot be obtained after all retries."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body
