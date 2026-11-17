"""Sign-in (Phase 5A, D68).

    GET  /api/auth/options   public: the directories to sign in with, and the local account
    POST /api/auth/login     {"login", "password", "directory"?} → session cookies + who
    POST /api/auth/logout    ends the session
    GET  /api/auth/me        who is signed in, their roles and permissions
    POST /api/auth/setup     {"code", "password"}: the local account's first password, with
                             the one-time code ``make admin-user ARGS=setup-code`` printed

A refused sign-in is always ``401 sign-in failed``; the reason is in the audit only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
async def me(who: Principal = Depends(principal)) -> dict[str, Any]:  # noqa: B008
    return who.public()


@router.post("/setup")
async def setup(body: Setup, request: Request,
                auth: AuthService = Depends(get_auth)) -> dict[str, Any]:  # noqa: B008
    try:
        await auth.complete_setup(body.code.get_secret_value(), body.password.get_secret_value(),
                                  ip=client_ip(request))
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True, "login": "gaia-admin"}
