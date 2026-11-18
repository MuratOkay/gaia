"""When scheduled work must stop (Phase 7C, P7-7; D98).

A job's long steps — profiling table by table, reasoning table by table — ask one question
between pieces of work: *should I stop?* ``StopSignal.reason()`` answers it from what the job
knows now, without a database call (the worker's heartbeat refreshes ``cancelled``):

* **cancelled** — an administrator asked (``make jobs ARGS="cancel ID"``, the *Runs* page);
* **the window closed** — the data source's run window (``profiling.window`` or the target's
  ``profiling_window``, A5) has ended;
* **the budget is spent** — the model tokens the run may use (``jobs.refresh_token_budget``,
  P7-7), counted by ``MeteredLLM`` around the model client.

Work already started finishes (a table half-reasoned is not written); what was not started
waits for the next run, which picks it up because nothing was recorded for it — the steps
are incremental by design (hashes, skip keys, P3-6's re-profiling rules).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gaia.llm.base import LLMClient, LLMResponse, Message
from gaia.profiling.budget import in_window

__all__ = ["MeteredLLM", "StopSignal"]


@dataclass
class StopSignal:
    budget_tokens: int = 0  # 0: no limit
    window: str | None = None
    cancelled: bool = False
    used_tokens: int = 0
    calls: int = 0
    stopped: str | None = None  # the first reason given, kept for the job's result
    clock: Any = None  # a callable returning "now" (tests); None: the real time
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, tokens: int) -> None:
        with self._lock:
            self.used_tokens += max(0, int(tokens))
            self.calls += 1

    def reason(self) -> str | None:
        """Why work must stop now, or None."""
        why = None
        if self.cancelled:
            why = "cancelled"
        elif self.window and not in_window(self.window, self._now()):
            why = f"the run window {self.window} closed"
        elif self.budget_tokens and self.used_tokens >= self.budget_tokens:
            why = f"the token budget of {self.budget_tokens:,} is spent"
        if why and self.stopped is None:
            self.stopped = why
        return why

    def _now(self) -> datetime | None:
        return self.clock() if self.clock else None


class MeteredLLM:
    """A model client that counts the tokens of every call into a ``StopSignal``."""

    def __init__(self, inner: LLMClient, signal: StopSignal) -> None:
        self.inner = inner
        self.signal = signal
        self.model = getattr(inner, "model", None)

    def complete(self, messages: list[Message], **kw: Any) -> LLMResponse:
        response = self.inner.complete(messages, **kw)
        usage = response.usage
        self.signal.add((usage.prompt_tokens or 0) + (usage.completion_tokens or 0))
        return response

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if close:
            close()
