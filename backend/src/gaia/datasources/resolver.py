"""Targets from the metadata store (Phase 4F, OUTLINE §24 "A TargetResolver").

``Settings.target(name)`` looks in ``config/gaia.yaml`` first — the bootstrap path,
unchanged — and, for any other name, here. So every tool and the API reach a data
source an administrator added without knowing where it came from.

A stored data source is usable only when its read-only probe passed (``verified``)
within ``datasources.reprobe_days``: "read-only is proven at onboarding and re-proven
periodically" (OUTLINE §24). Anything else raises ``TargetUnusable`` with the reason
and the command that fixes it. Resolved targets are cached for ``CACHE_S`` seconds.

Synchronous on purpose: ``Settings.target`` is called from synchronous code all over
the pipeline. One indexed row read on a cache miss.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from gaia.core.config import Settings, TargetConfig

__all__ = ["CACHE_S", "TargetUnusable", "clear_cache", "resolve", "target_from"]

CACHE_S = 60.0
_cache: dict[str, tuple[float, TargetConfig]] = {}
_lock = threading.Lock()


class TargetUnusable(KeyError):
    """A stored data source exists but may not be used (not verified, stale, disabled)."""

    def __str__(self) -> str:  # KeyError quotes its argument; say it plainly
        return str(self.args[0]) if self.args else ""


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def target_from(connection: dict[str, Any], password: str) -> TargetConfig:
    """The ``TargetConfig`` of stored, non-secret settings and the decrypted password."""
    return TargetConfig.model_validate({**connection, "password": password})


def _fetch(name: str) -> tuple | None:
    import psycopg

    from gaia.db.engine import metadata_url

    url = metadata_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            return conn.execute(
                "SELECT id, origin, status, connection, secret, secret_key_id, probed_at "
                "FROM catalog.data_sources WHERE name = %s", (name,)).fetchone()
    except Exception:  # no store, or a store before migration 0010: nothing to resolve
        return None


def resolve(name: str, settings: Settings) -> TargetConfig | None:
    """The stored data source ``name``, or None when there is none."""
    with _lock:
        hit = _cache.get(name)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
    row = _fetch(name)
    if row is None:
        return None
    ds_id, origin, status, connection, secret, kid, probed_at = row
    if origin != "admin":
        return None  # a config target's row: the config is its source
    hint = f"make datasource ARGS='probe {name}'"
    if status != "verified":
        raise TargetUnusable(f"data source {name!r} is {status}; it is usable once its "
                             f"read-only probe passes ({hint})")
    max_age = timedelta(days=settings.datasources.reprobe_days)
    if probed_at is None or datetime.now(UTC) - probed_at > max_age:
        raise TargetUnusable(f"data source {name!r} was last proven read-only more than "
                             f"{settings.datasources.reprobe_days} days ago ({hint})")
    from gaia.datasources.crypto import decrypt

    target = target_from(connection or {}, decrypt(bytes(secret), kid, uuid.UUID(str(ds_id))))
    with _lock:
        _cache[name] = (time.monotonic(), target)
    return target
