"""DeepSeek client (OpenAI-compatible chat completions).

Behavioural notes that matter for correctness, from the DeepSeek API docs:

* Thinking mode is **on by default**; it is toggled with
  ``{"thinking": {"type": "enabled" | "disabled"}}`` in the body.
* In thinking mode the chain of thought comes back as ``reasoning_content``,
  a sibling of ``content``.
* Thinking mode ignores ``temperature``, ``presence_penalty`` and
  ``frequency_penalty`` — setting them is a silent no-op, so we omit them.
* JSON mode needs ``response_format={"type":"json_object"}`` *and* the word
  "json" plus an example in the prompt. It can still return blank content,
  which we retry.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

from gaia.core.config import LLMConfig
from gaia.llm.base import LLMError, LLMResponse, Message, Usage


def _retry_after(response: httpx.Response) -> float | None:
    """Parse ``Retry-After`` (seconds form). Gateways rarely send it, so this
    is a bonus rather than a dependency."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class DeepSeekClient:
    def __init__(self, config: LLMConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(float(config.timeout_s), connect=20.0)
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> DeepSeekClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    @property
    def _url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def _body(
        self,
        messages: list[Message],
        *,
        json_mode: bool,
        thinking: bool,
        max_tokens: int | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.as_dict() for m in messages],
            "max_tokens": max_tokens or self.config.max_output_tokens,
            "stream": False,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if not thinking and temperature is not None:
            # Only meaningful outside thinking mode.
            body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _parse(self, payload: dict[str, Any], latency_ms: int, attempts: int) -> LLMResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("response contained no choices", body=str(payload)[:500])
        message = choices[0].get("message") or {}
        usage_raw = payload.get("usage") or {}
        prompt_details = usage_raw.get("prompt_tokens_details") or {}
        completion_details = usage_raw.get("completion_tokens_details") or {}
        usage = Usage(
            prompt_tokens=usage_raw.get("prompt_tokens", 0) or 0,
            completion_tokens=usage_raw.get("completion_tokens", 0) or 0,
            total_tokens=usage_raw.get("total_tokens", 0) or 0,
            cached_tokens=(
                usage_raw.get("prompt_cache_hit_tokens")
                or prompt_details.get("cached_tokens")
                or 0
            ),
            reasoning_tokens=completion_details.get("reasoning_tokens") or 0,
            cache_created_tokens=prompt_details.get("created_cache_tokens") or 0,
        )
        return LLMResponse(
            content=message.get("content") or "",
            # Official DeepSeek uses ``reasoning_content``; OpenAI-compatible
            # gateways (including the Evren proxy) use ``reasoning``. Accept both.
            reasoning=message.get("reasoning_content") or message.get("reasoning"),
            model=payload.get("model", self.config.model),
            finish_reason=choices[0].get("finish_reason"),
            usage=usage,
            latency_ms=latency_ms,
            attempts=attempts,
            request_id=payload.get("id"),
        )



    def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        thinking: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        if not self.config.api_key:
            raise LLMError("DEEPSEEK_API_KEY is not set")

        body = self._body(
            messages,
            json_mode=json_mode,
            thinking=thinking,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        max_attempts = max(1, self.config.retry.max_attempts)
        # 429 (concurrency ceiling) and 503 (model overloaded) both mean "the
        # gateway has no capacity right now". They clear on their own, so they
        # get more patience than a hard error — but not an unbounded loop.
        max_transient_attempts = max(1, self.config.retry.max_attempts_rate_limit)
        last_error: Exception | None = None
        attempt = 0
        transient_attempts = 0

        while True:
            attempt += 1
            started = time.perf_counter()
            try:
                response = self._client.post(self._url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                self._sleep(attempt)
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)

            if response.status_code in (429, 502, 503, 504):
                transient_attempts += 1
                last_error = LLMError(
                    f"http {response.status_code} (gateway capacity)",
                    status=response.status_code,
                    body=response.text[:500],
                )
                if transient_attempts >= max_transient_attempts:
                    break
                self._sleep(transient_attempts, retry_after=_retry_after(response))
                continue

            if response.status_code >= 500:
                last_error = LLMError(
                    f"http {response.status_code}",
                    status=response.status_code,
                    body=response.text[:500],
                )
                if attempt >= max_attempts:
                    break
                self._sleep(attempt)
                continue

            if response.status_code != 200:
                raise LLMError(
                    f"http {response.status_code}",
                    status=response.status_code,
                    body=response.text[:1000],
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise LLMError("response was not valid JSON", body=response.text[:500]) from exc

            result = self._parse(payload, latency_ms, attempt)

            if result.is_blank and self.config.retry.retry_on_blank_content:
                # JSON mode silently blanks unparseable output; retry.
                last_error = LLMError("blank content returned")
                if attempt >= max_attempts:
                    break
                self._sleep(attempt)
                continue

            return result

        raise LLMError(f"completion failed after {attempt} attempt(s): {last_error}")

    def _sleep(self, attempt: int, *, retry_after: float | None = None) -> None:
        """Exponential backoff with a cap and jitter.

        Jitter matters here: with several workers hitting a 4-concurrency ceiling
        at once, fixed sleeps make them retry in lockstep and collide again.
        """
        base = max(0.1, self.config.retry.backoff_base_s)
        delay = min(base ** attempt, self.config.retry.max_backoff_s)
        if retry_after is not None:
            delay = max(delay, retry_after)
        delay = min(delay, self.config.retry.max_backoff_s)
        time.sleep(delay * (0.5 + random.random() * 0.5))


def build_client(config: LLMConfig) -> DeepSeekClient:
    """Reasoning client for ``llm.provider``.

    DeepSeek is the only reasoning provider. Ollama serves embeddings only
    (``gaia.llm.embeddings``). Other providers plug in behind ``LLMClient``.
    """
    if config.provider != "deepseek":
        raise NotImplementedError(
            f"reasoning provider {config.provider!r} is not supported; "
            "use 'deepseek' (Ollama is the embeddings provider only)"
        )
    return DeepSeekClient(config)
