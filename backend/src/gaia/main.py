"""Gaia FastAPI application.

Phase 0 surface: ``/health`` (infrastructure + config diagnostics, public) and
``/api/meta`` (redacted, non-secret configuration echo; administrators). Phase 4E: the
chat API (``gaia.api.chat``). Phase 5A: sign-in (``gaia.api.auth``), the administrator's
API (``gaia.api.admin``) and the auditor's reports (``gaia.api.audit``); every endpoint but
``/health`` and the sign-in ones needs a session. Serve on 127.0.0.1 until TLS is in
place (P5-9; ``make dev-api``).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gaia import __version__
from gaia.api.access import router as access_router
from gaia.api.admin import router as admin_router
from gaia.api.audit import router as audit_router
from gaia.api.auth import router as auth_router
from gaia.api.chat import router as chat_router
from gaia.api.evaluation import router as evaluation_router
from gaia.api.jobs import router as jobs_router
from gaia.api.models import router as models_router
from gaia.api.queries import router as queries_router
from gaia.api.review import router as review_router
from gaia.auth.deps import require
from gaia.core.config import get_settings
from gaia.db.engine import asyncpg_dsn, dispose_engine
from gaia.db.revision import expected_revision, explain, store_revision

logger = logging.getLogger("gaia")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=getattr(logging, settings.app.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("gaia %s starting (target=%s)", __version__, settings.app.default_target)
    if settings.unresolved_env:
        logger.warning("unresolved env placeholders: %s", ", ".join(settings.unresolved_env))
    schema = await _check_schema()
    if not schema.get("ok", True) and schema.get("advice"):
        logger.error("%s", schema["advice"])  # D76: say it before the first request fails
    yield
    await dispose_engine()


app = FastAPI(
    title="Gaia",
    version=__version__,
    description="Metadata-driven semantic agent for business question answering.",
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(admin_router)  # the admin role (Phase 5A)
app.include_router(audit_router)  # the auditor role (Phase 5A)
app.include_router(review_router)  # SMEs of a data source (Phase 5B)
app.include_router(access_router)  # data categories: the admin role (Phase 5C)
app.include_router(queries_router)  # the SQL of answers, runs (Phase 5D, 5E)
app.include_router(evaluation_router)  # golden runs and the quality report (Phase 6A)
app.include_router(jobs_router)  # jobs, schedules, alerts (Phase 7C)
app.include_router(models_router)  # models registered, trialled, switched (Phase 7D)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _check_metadata_db() -> dict[str, Any]:
    try:
        conn = await asyncpg.connect(dsn=asyncpg_dsn(), timeout=5)
        try:
            row = await conn.fetchrow(
                "SELECT current_database() AS db, "
                "(SELECT count(*) FROM pg_extension WHERE extname = 'vector') AS has_vector, "
                "(SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm') AS has_trgm"
            )
        finally:
            await conn.close()
        ok = bool(row["has_vector"]) and bool(row["has_trgm"])
        return {
            "ok": ok,
            "database": row["db"],
            "extensions": {"vector": bool(row["has_vector"]), "pg_trgm": bool(row["has_trgm"])},
        }
    except Exception as exc:  # surfaced to the operator, not raised
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def _check_schema() -> dict[str, Any]:
    """The store's migration against this build's (D76)."""
    try:
        conn = await asyncpg.connect(dsn=asyncpg_dsn(), timeout=5)
        try:
            current = await store_revision(conn)
        finally:
            await conn.close()
    except Exception as exc:  # the metadata_db check reports the connection itself
        return {"ok": False, "error": f"{type(exc).__name__}"}
    expected = expected_revision()
    advice = explain(current, expected)
    return {"ok": advice is None, "current": current, "expected": expected,
            **({"advice": advice} if advice else {})}


async def _check_redis() -> dict[str, Any]:
    client = None
    try:
        client = aioredis.from_url(settings.cache.redis_url, socket_connect_timeout=3)
        pong = await client.ping()
        return {"ok": bool(pong)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if client is not None:
            await client.aclose()


@app.get("/health")
async def health() -> dict[str, Any]:
    checks = {
        "metadata_db": await _check_metadata_db(),
        "schema": await _check_schema(),
        "redis": await _check_redis(),
    }
    healthy = all(c.get("ok") for c in checks.values())
    return {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "checks": checks,
        "unresolved_env": settings.unresolved_env,
    }


@app.get("/api/meta", dependencies=[Depends(require("admin"))])
async def meta() -> dict[str, Any]:
    target = settings.target()
    return {
        "app": settings.app.model_dump(),
        "target": {
            "name": settings.app.default_target,
            "dialect": target.dialect,
            "host": target.host,
            "port": target.port,
            "database": target.database,
            "username": target.username,
            "capabilities": target.capabilities.model_dump(),
            "limits": target.limits.model_dump(),
        },
        "llm": {
            "provider": settings.llm.provider,
            "model": settings.llm.model,
            "base_url": settings.llm.base_url,
            "json_mode": settings.llm.json_mode,
            "api_key_set": bool(settings.llm.api_key),
        },
        "embeddings": {
            "provider": settings.embeddings.provider,
            "model": settings.embeddings.model,
            "dimensions": settings.embeddings.dimensions,
        },
        "unresolved_env": settings.unresolved_env,
    }
