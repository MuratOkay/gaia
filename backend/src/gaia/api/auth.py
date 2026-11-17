"""Sign-in (Phase 5A, D68).

    GET  /api/auth/options   public: the directories to sign in with, and the local account
    POST /api/auth/login     {"login", "password", "directory"?} → session cookies + who
    POST /api/auth/logout    ends the session
    GET  /api/auth/me        who is signed in, their roles and permissions
    POST /api/auth/setup     {"code", "password"}: the local account's first password, with
                             the one-time code ``make admin-user ARGS=setup-code`` printed
    GET  /api/auth/oidc/start?directory=NAME   Entra ID (5A.2): off to Microsoft
    GET  /api/auth/oidc/callback               …and back: signed in, or not

A refused sign-in is always ``401 sign-in failed`` (for Entra ID: a page that says so);
the reason is in the audit only.

Entra ID's callback is reached by a redirect *from Microsoft*, a cross-site navigation, so:
the cookie binding the sign-in to this browser (``gaia_oidc``) is ``SameSite=Lax`` (Strict
would not come back), and the callback answers with a small page that moves on to the app —
a navigation the site starts itself, so the ``SameSite=Strict`` session cookie is sent with
the first page (after a redirect chain that began at Microsoft it would not be).
"""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, SecretStr

from gaia.auth.deps import (
    SESSION_COOKIE,
    clear_cookies,
    client_ip,
    get_auth,
    principal,
    set_cookies,
)
from gaia.auth.service import AuthError, AuthService, Principal, SignInRefused

__all__ = ["router"]

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Login(BaseModel):
    login: str
    password: SecretStr  # masked in repr, errors and logs
    directory: str | None = None


class Setup(BaseModel):
    code: SecretStr
    password: SecretStr


@router.get("/options")
async def options(auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    return await auth.sign_in_options()


@router.post("/login")
async def login(body: Login, request: Request, response: Response,
                auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    try:
        who, token, csrf = await auth.sign_in(
            body.login, body.password.get_secret_value(), directory=body.directory,
            ip=client_ip(request), user_agent=request.headers.get("user-agent"))
    except SignInRefused:
        raise HTTPException(401, "sign-in failed") from None
    set_cookies(response, auth, token, csrf)
    return who.public() | {"csrf": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response,
                 who: Principal = Depends(principal),  # noqa: B008  (CSRF-checked)
                 auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    await auth.sign_out(request.cookies.get(SESSION_COOKIE))
    clear_cookies(response, auth)
    return {"signed_out": who.login}


@router.get("/me")
async def me(who: Principal = Depends(principal),  # noqa: B008
             auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    doc = who.public()
    if who.effective.categories:  # names, not ids, for the screens (5C)
        from gaia.auth.service import _category_names

        async with auth.factory() as s:
            doc["category_names"] = await _category_names(s, who.effective.categories)
    else:
        doc["category_names"] = []
    return doc


@router.post("/setup")
async def setup(body: Setup, request: Request,
                auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    try:
        await auth.complete_setup(body.code.get_secret_value(), body.password.get_secret_value(),
                                  ip=client_ip(request))
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True, "login": "gaia-admin"}


OIDC_COOKIE = "gaia_oidc"
OIDC_PATH = "/api/auth/oidc"


def _page(target: str, title: str) -> str:
    """A page that moves the browser on by itself (a same-site navigation)."""
    t, u = escape(title), escape(target, quote=True)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{t}</title>'
            f'<meta http-equiv="refresh" content="0;url={u}"></head>'
            f'<body><p>{t} — <a href="{u}">continue</a></p></body></html>')


@router.get("/oidc/start")
async def oidc_start(directory: str, request: Request,
                     auth: AuthService = Depends(get_auth)):  # noqa: B008
    try:
        url, binding = await auth.oidc_start(directory, ip=client_ip(request))
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from None
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(OIDC_COOKIE, binding, max_age=auth.cfg.oidc_minutes * 60, httponly=True,
                        samesite="lax", secure=auth.cfg.cookie_secure, path=OIDC_PATH)
    return response


@router.get("/oidc/callback")
async def oidc_callback(request: Request, state: str | None = None, code: str | None = None,
                        error: str | None = None,
                        auth: AuthService = Depends(get_auth)):  # noqa: B008
    try:
        _who, token, csrf, after = await auth.oidc_finish(
            state=state, code=code, binding=request.cookies.get(OIDC_COOKIE), error=error,
            ip=client_ip(request), user_agent=request.headers.get("user-agent"))
    except SignInRefused:
        response = HTMLResponse(_page("/?signin=failed", "Sign-in failed"), status_code=401)
    else:
        response = HTMLResponse(_page(after, "Signed in"))
        set_cookies(response, auth, token, csrf)
    response.delete_cookie(OIDC_COOKIE, path=OIDC_PATH, samesite="lax",
                           secure=auth.cfg.cookie_secure)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"  # the code never leaves in a Referer
    return response
