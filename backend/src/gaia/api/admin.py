"""The administrator's API (Phase 4F data sources; Phase 5A directories, roles, grants).

Every endpoint needs a signed-in **admin** (D68) — the ``datasources.admin_api`` switch
that kept these off until there was a login is no longer read. Every change is audited
with the administrator's login: data sources in ``audit.connection_change`` (D65), access
in the hash-chained ``audit.auth_event`` (D70).

    GET    /api/admin/data-sources                      (4F, unchanged otherwise)
    POST   /api/admin/data-sources                      {spec…, "password"}
    POST   /api/admin/data-sources/{name}/probe
    PATCH  /api/admin/data-sources/{name}
    POST   /api/admin/data-sources/{name}/disable | /enable
    DELETE /api/admin/data-sources/{name}

    GET    /api/admin/directories
    POST   /api/admin/directories                       {"name", "kind", "settings", "password"}
    PATCH  /api/admin/directories/{name}                {"settings"?, "password"?} → unverified
    POST   /api/admin/directories/{name}/probe          "test connection"
    POST   /api/admin/directories/{name}/sync           users and roles, now
    POST   /api/admin/directories/{name}/disable | /enable
    GET    /api/admin/directories/{name}/roles

    GET    /api/admin/accounts                          who, their directory roles, what they may do
    GET    /api/admin/grants?include_revoked=true
    POST   /api/admin/grants                            {"privilege", "directory_role_id" | "login"
                                                         [+ "directory"], "data_source"?,
                                                         "category"? (5C), "note"?}
    DELETE /api/admin/grants/{id}                       revoke (kept, dated, signed)
    GET    /api/admin/local-account                     the break-glass account's state
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr

from gaia.auth.deps import get_auth, require
from gaia.auth.service import AuthError, AuthService, Principal
from gaia.datasources.crypto import CredentialError
from gaia.datasources.service import DataSourceError, DataSourceService, DataSourceSpec

__all__ = ["router"]

router = APIRouter(prefix="/api/admin", tags=["admin"])
ADMIN = require("admin")


# -- data sources (4F) -----------------------------------------------------------------------


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


async def _sources(who: Principal, work, auth: AuthService):
    async with auth.factory() as session:
        try:
            result = await work(DataSourceService(session, auth.settings, actor=who.login))
        except DataSourceError as exc:
            raise HTTPException(400, str(exc)) from None
        except CredentialError as exc:
            raise HTTPException(503, f"credentials unavailable: {exc}") from None
        await session.commit()
        return result


@router.get("/data-sources")
async def list_sources(who: Principal = Depends(ADMIN),  # noqa: B008
                       auth: AuthService = Depends(get_auth)) -> list[dict[str, Any]]:  # noqa: B008
    return await _sources(who, lambda s: s.list(), auth)


@router.post("/data-sources", status_code=201)
async def add_source(body: NewDataSource, who: Principal = Depends(ADMIN),  # noqa: B008
                     auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    spec = DataSourceSpec.model_validate(body.model_dump(exclude={"password"}))
    return await _sources(who, lambda s: s.add(spec, body.password.get_secret_value()), auth)


@router.post("/data-sources/{name}/probe")
async def probe_source(name: str, who: Principal = Depends(ADMIN),  # noqa: B008
                       auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _sources(who, lambda s: s.probe(name), auth)


@router.patch("/data-sources/{name}")
async def change_source(name: str, body: Changes, who: Principal = Depends(ADMIN),  # noqa: B008
                        auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    changes = body.model_dump(exclude_none=True, exclude={"password"})
    secret = body.password.get_secret_value() if body.password else None
    return await _sources(who, lambda s: s.update(name, changes, secret), auth)


@router.post("/data-sources/{name}/disable")
async def disable_source(name: str, who: Principal = Depends(ADMIN),  # noqa: B008
                         auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _sources(who, lambda s: s.set_enabled(name, False), auth)


@router.post("/data-sources/{name}/enable")
async def enable_source(name: str, who: Principal = Depends(ADMIN),  # noqa: B008
                        auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _sources(who, lambda s: s.set_enabled(name, True), auth)


@router.delete("/data-sources/{name}")
async def remove_source(name: str, who: Principal = Depends(ADMIN),  # noqa: B008
                        auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _sources(who, lambda s: s.remove(name), auth)


# -- directories, roles, accounts, grants (5A) -------------------------------------------------


class NewDirectory(BaseModel):
    name: str
    kind: str = "ldap"
    settings: dict[str, Any]
    password: SecretStr  # the service account's


class DirectoryChanges(BaseModel):
    settings: dict[str, Any] | None = None
    password: SecretStr | None = None


class NewGrant(BaseModel):
    privilege: str
    directory_role_id: uuid.UUID | None = None
    login: str | None = None
    directory: str | None = None
    data_source: str | None = None
    category: str | None = None  # privilege "category" (5C): its name or id
    note: str | None = None


async def _auth(work):
    try:
        return await work
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from None
    except CredentialError as exc:
        raise HTTPException(503, f"credentials unavailable: {exc}") from None


@router.get("/directories")
async def directories(_who: Principal = Depends(ADMIN),  # noqa: B008
                      auth: AuthService = Depends(get_auth)) -> list[dict[str, Any]]:  # noqa: B008
    return await auth.directories()


@router.post("/directories", status_code=201)
async def add_directory(body: NewDirectory, who: Principal = Depends(ADMIN),  # noqa: B008
                        auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _auth(auth.add_directory(body.name, body.kind, body.settings,
                                          body.password.get_secret_value(), actor=who.login))


@router.patch("/directories/{name}")
async def change_directory(name: str, body: DirectoryChanges,
                           who: Principal = Depends(ADMIN),  # noqa: B008
                           auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _auth(auth.update_directory(
        name, actor=who.login, settings=body.settings,
        password=body.password.get_secret_value() if body.password else None))


@router.post("/directories/{name}/probe")
async def probe_directory(name: str, who: Principal = Depends(ADMIN),  # noqa: B008
                          auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _auth(auth.probe_directory(name, actor=who.login))


@router.post("/directories/{name}/sync")
async def sync_directory(name: str, who: Principal = Depends(ADMIN),  # noqa: B008
                         auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _auth(auth.sync_directory(name, actor=who.login))


@router.post("/directories/{name}/disable")
async def disable_directory(name: str, who: Principal = Depends(ADMIN),  # noqa: B008
                            auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _auth(auth.set_directory_enabled(name, False, actor=who.login))


@router.post("/directories/{name}/enable")
async def enable_directory(name: str, who: Principal = Depends(ADMIN),  # noqa: B008
                           auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _auth(auth.set_directory_enabled(name, True, actor=who.login))


@router.get("/directories/{name}/roles")
async def directory_roles(name: str, include_gone: bool = False,
                          _who: Principal = Depends(ADMIN),  # noqa: B008
                          auth: AuthService = Depends(get_auth)) -> list[dict[str, Any]]:  # noqa: B008
    return await _auth(auth.roles(name, include_gone=include_gone))


@router.get("/accounts")
async def accounts(_who: Principal = Depends(ADMIN),  # noqa: B008
                   auth: AuthService = Depends(get_auth)) -> list[dict[str, Any]]:  # noqa: B008
    return await auth.accounts()


@router.get("/grants")
async def grants(include_revoked: bool = False, _who: Principal = Depends(ADMIN),  # noqa: B008
                 auth: AuthService = Depends(get_auth)) -> list[dict[str, Any]]:  # noqa: B008
    return await auth.grants(include_revoked=include_revoked)


@router.post("/grants", status_code=201)
async def add_grant(body: NewGrant, who: Principal = Depends(ADMIN),  # noqa: B008
                    auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    account = (body.directory, body.login) if body.login else None
    return await _auth(auth.grant(body.privilege, actor=who.login,
                                  directory_role_id=body.directory_role_id, account=account,
                                  data_source=body.data_source, note=body.note,
                                  category=body.category))


@router.delete("/grants/{grant_id}")
async def revoke_grant(grant_id: uuid.UUID, note: str | None = None,
                       who: Principal = Depends(ADMIN),  # noqa: B008
                       auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await _auth(auth.revoke(grant_id, actor=who.login, note=note))


@router.get("/local-account")
async def local_account(_who: Principal = Depends(ADMIN),  # noqa: B008
                        auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await auth.local_status()
