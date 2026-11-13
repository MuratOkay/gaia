"""Async engine helpers for the Gaia metadata store."""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gaia.core.config import ENV_PATH

DEFAULT_DSN = "postgresql+asyncpg://gaia:gaia@localhost:5433/gaia"

_dotenv_loaded = False


def _ensure_dotenv() -> None:
    """Load ``.env`` before reading the environment.

    Alembic and CLI tools use this module directly, bypassing the settings
    loader, so the file must be read here too.
    """
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv(ENV_PATH, override=False)
        _dotenv_loaded = True


def metadata_url() -> str:
    """SQLAlchemy URL for the metadata store, with the async driver."""
    _ensure_dotenv()
    raw = os.environ.get("GAIA_METADATA_URL", DEFAULT_DSN)
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    return raw


def asyncpg_dsn() -> str:
    """Bare ``postgresql://`` DSN suitable for asyncpg."""
    parts = urlsplit(metadata_url())
    scheme = parts.scheme.split("+", 1)[0]
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))



_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(metadata_url(), pool_pre_ping=True, pool_size=5)
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
