"""FastAPI dependencies: who is asking, and may they (Phase 5A, D68).

* The session token travels in an **HttpOnly, SameSite=Strict** cookie (``gaia_session``)
  that scripts cannot read; ``Secure`` once the API is behind TLS (``auth.cookie_secure``,
  P5-9).
* **CSRF**: every request that changes something (anything but GET/HEAD/OPTIONS) must
  repeat the session's CSRF token in the ``X-CSRF-Token`` header. The token is handed to the
  page in a readable cookie (``gaia_csrf``); another site can neither read it nor, with
  SameSite=Strict, send the session cookie at all — two independent locks.
* ``require("admin")`` etc. answer 401 without a session and 403 with the wrong role; a 403
  is recorded in the audit (refusals are part of the auditor's report, D70).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException, Request, Response

from gaia.auth import audit
from gaia.auth.service import AuthService, NotSignedIn, Principal

__all__ = ["CSRF_COOKIE", "CSRF_HEADER", "SESSION_COOKIE", "client_ip", "clear_cookies",
           "get_auth", "principal", "require", "require_chat", "set_cookies"]

SESSION_COOKIE = "gaia_session"
CSRF_COOKIE = "gaia_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE = {"GET", "HEAD", "OPTIONS"}


def get_auth(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth", None)
    if service is None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from gaia.core.config import get_settings
        from gaia.db.engine import get_engine

        service = request.app.state.auth = AuthService(
            get_settings(), async_sessionmaker(get_engine(), expire_on_commit=False))
    return service


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def set_cookies(response: Response, auth: AuthService, token: str, csrf: str) -> None:
    cfg = auth.cfg
    age = cfg.session_max_hours * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=age, httponly=True, samesite="strict",
                        secure=cfg.cookie_secure, path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=age, httponly=False, samesite="strict",
                        secure=cfg.cookie_secure, path="/")


def clear_cookies(response: Response, auth: AuthService) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", samesite="strict", secure=auth.cfg.cookie_secure)


async def principal(request: Request, auth: AuthService = Depends(get_auth)) -> Principal:  # noqa: B008
    try:
        return await auth.resolve(request.cookies.get(SESSION_COOKIE),
                                  csrf=request.headers.get(CSRF_HEADER),
                                  unsafe=request.method not in SAFE, ip=client_ip(request))
    except NotSignedIn as exc:
        raise HTTPException(401, str(exc)) from None


async def _refuse(auth: AuthService, request: Request, who: Principal, needed: str) -> None:
    async with auth.factory() as s:
        await audit.record(s, "forbidden", actor=who.login,
                           subject=f"{request.method} {request.url.path}",
                           detail={"needed": needed, "roles": sorted(who.effective.roles)},
                           ip=client_ip(request))
        await s.commit()
    raise HTTPException(403, f"this needs the {needed} role")


def require(*roles: str) -> Callable[..., Any]:
    """A dependency: the principal, if they hold one of ``roles``."""
    needed = " or ".join(roles)

    async def dependency(request: Request, who: Principal = Depends(principal),  # noqa: B008
                         auth: AuthService = Depends(get_auth)) -> Principal:  # noqa: B008
        if not who.has(*roles):
            await _refuse(auth, request, who, needed)
        return who

    return dependency


async def require_chat(request: Request, who: Principal = Depends(principal),  # noqa: B008
                       auth: AuthService = Depends(get_auth)) -> Principal:  # noqa: B008
    """The chat: the Gaia roles in ``auth.chat_roles`` (admin and sme until need-to-know,
    5C, adds user)."""
    roles = tuple(auth.cfg.chat_roles)
    if not who.has(*roles):
        await _refuse(auth, request, who, " or ".join(roles))
    return who
