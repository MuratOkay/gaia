"""Ephemeral response caches (Redis).

Redis is a cache only (OUTLINE §14): PostgreSQL is the source of truth, and
Gaia must behave correctly after a flush. So every cache here is optional.
If Redis is unreachable the cache logs one warning and acts as a permanent
miss instead of failing the run.

Key formats follow OUTLINE §14 and carry every input that affects the value:

    llm:{model}:{prompt_version}:{sha256(messages + params)}
    embed:{model}:{sha256(text)}
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "KVCache",
    "MemoryCache",
    "NullCache",
    "RedisCache",
    "build_cache",
    "embedding_cache_key",
    "llm_cache_key",
]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def llm_cache_key(
    *, model: str, prompt_version: str, messages: list[dict[str, str]], params: dict[str, Any]
) -> str:
    """Stable key for one completion request.

    ``params`` must include everything that changes the answer (thinking,
    json_mode, max_tokens, temperature). Canonical JSON keeps the key
    independent of dict ordering.
    """
    material = json.dumps(
        {"messages": messages, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"llm:{model}:{prompt_version}:{_sha256(material)}"


def embedding_cache_key(*, model: str, text: str) -> str:
    return f"embed:{model}:{_sha256(text)}"


class KVCache(Protocol):
    def get_many(self, keys: list[str]) -> list[str | None]: ...

    def set_many(self, items: dict[str, str], ttl_s: int) -> None: ...


class NullCache:
    """Always misses. Used for ``--no-cache`` and when Redis is unavailable."""

    def get_many(self, keys: list[str]) -> list[str | None]:
        return [None] * len(keys)

    def set_many(self, items: dict[str, str], ttl_s: int) -> None:
        return None


class MemoryCache:
    """In-process cache for tests. Ignores TTLs."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get_many(self, keys: list[str]) -> list[str | None]:
        return [self.data.get(k) for k in keys]

    def set_many(self, items: dict[str, str], ttl_s: int) -> None:
        self.data.update(items)


class RedisCache:
    """Redis-backed cache that degrades to a miss on any Redis error."""

    def __init__(self, url: str, *, client: Any = None) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(
                url, socket_connect_timeout=2, socket_timeout=5, decode_responses=True
            )
        self._client = client
        self._disabled = False

    def _fail(self, exc: Exception) -> None:
        if not self._disabled:
            logger.warning("redis cache disabled for this run: %s: %s", type(exc).__name__, exc)
        self._disabled = True

    def get_many(self, keys: list[str]) -> list[str | None]:
        if self._disabled or not keys:
            return [None] * len(keys)
        try:
            return list(self._client.mget(keys))
        except Exception as exc:
            self._fail(exc)
            return [None] * len(keys)

    def set_many(self, items: dict[str, str], ttl_s: int) -> None:
        if self._disabled or not items:
            return
        try:
            pipe = self._client.pipeline(transaction=False)
            for key, value in items.items():
                pipe.set(key, value, ex=ttl_s)
            pipe.execute()
        except Exception as exc:
            self._fail(exc)


def build_cache(redis_url: str, *, enabled: bool = True) -> KVCache:
    if not enabled or not redis_url:
        return NullCache()
    return RedisCache(redis_url)
