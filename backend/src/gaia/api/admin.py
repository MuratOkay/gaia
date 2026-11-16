"""Admin endpoints for data sources (Phase 4F) — **off** unless
``datasources.admin_api: true``: they write credentials, and the API has no login
until P5-1. The CLI (``make datasource``) is the admin path meanwhile; the Phase 5 UI
will use these.

    GET    /api/admin/data-sources                 every data source (never a password)
    POST   /api/admin/data-sources                 {spec…, "password"} → unverified
    POST   /api/admin/data-sources/{name}/probe    the read-only probe → verified | failed
    PATCH  /api/admin/data-sources/{name}          {changes…, "password"?} → unverified
    POST   /api/admin/data-sources/{name}/disable  | /enable
    DELETE /api/admin/data-sources/{name}          tombstone, credential deleted
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, SecretStr

from gaia.datasources.crypto import CredentialError
from gaia.datasources.service import DataSourceError, DataSourceService, DataSourceSpec

__all__ = ["router"]

router = APIRouter(prefix="/api/admin/data-sources", tags=["admin"])


class NewDataSource(DataSourceSpec):
    password: SecretStr  # masked in repr, str, errors and logs


class Changes(BaseModel):
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    options: dict[str, Any] | None = None
    limits: dict[str, Any] | None = None
    profiling_enabled: bool | None = None
    profiling_window: str | None = None
    password: SecretStr | None = None


def _chat(request: Request):
    from gaia.api.chat import get_chat

    chat = get_chat(request)
    if not chat.settings.datasources.admin_api:
        raise HTTPException(404, "the admin API is off (datasources.admin_api)")
    return chat


async def _do(chat, work):
    async with chat.factory() as session:
        try:
            result = await work(DataSourceService(session, chat.settings))
        except DataSourceError as exc:
            raise HTTPException(400, str(exc)) from None
        except CredentialError as exc:
            raise HTTPException(503, f"credentials unavailable: {exc}") from None
        await session.commit()
        return result


@router.get("")
async def list_sources(chat=Depends(_chat)) -> list[dict[str, Any]]:  # noqa: B008
    return await _do(chat, lambda s: s.list())


@router.post("", status_code=201)
async def add_source(body: NewDataSource, chat=Depends(_chat)) -> dict[str, Any]:  # noqa: B008
    spec = DataSourceSpec.model_validate(body.model_dump(exclude={"password"}))
    return await _do(chat, lambda s: s.add(spec, body.password.get_secret_value()))


@router.post("/{name}/probe")
async def probe_source(name: str, chat=Depends(_chat)) -> dict[str, Any]:  # noqa: B008
    return await _do(chat, lambda s: s.probe(name))


@router.patch("/{name}")
async def change_source(name: str, body: Changes, chat=Depends(_chat)) -> dict[str, Any]:  # noqa: B008
    changes = body.model_dump(exclude_none=True, exclude={"password"})
    secret = body.password.get_secret_value() if body.password else None
    return await _do(chat, lambda s: s.update(name, changes, secret))


@router.post("/{name}/disable")
async def disable_source(name: str, chat=Depends(_chat)) -> dict[str, Any]:  # noqa: B008
    return await _do(chat, lambda s: s.set_enabled(name, False))


@router.post("/{name}/enable")
async def enable_source(name: str, chat=Depends(_chat)) -> dict[str, Any]:  # noqa: B008
    return await _do(chat, lambda s: s.set_enabled(name, True))


@router.delete("/{name}")
async def remove_source(name: str, chat=Depends(_chat)) -> dict[str, Any]:  # noqa: B008
    return await _do(chat, lambda s: s.remove(name))
