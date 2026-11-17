"""Phase 5A.2 over HTTP on the real application: sign-in with Microsoft Entra ID (OIDC,
authorization code + PKCE) against the offline Microsoft of ``tests/unit/_fake_entra.py``."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from _fake_entra import SECRET, FakeMicrosoft, settings  # noqa: E402
from test_auth_pg import Clock  # noqa: E402

from gaia.auth.entra import EntraDirectory, EntraSettings  # noqa: E402
from gaia.auth.service import AuthService, SignInRefused  # noqa: E402
from gaia.core.config import AppSection, Settings  # noqa: E402
from gaia.datasources.crypto import new_key  # noqa: E402
from gaia.db.models.audit import AuthEvent  # noqa: E402
from gaia.db.models.auth import PendingLogin  # noqa: E402


@pytest.fixture()
async def corp(pg_session_factory, monkeypatch):
    monkeypatch.setenv("GAIA_MASTER_KEY", new_key())
    from gaia.main import app

    fake, clock = FakeMicrosoft(), Clock()

    def factory(row, secret):
        return EntraDirectory(EntraSettings.model_validate(row.settings), secret,
                              http=httpx.Client(transport=httpx.MockTransport(fake.handler)))

    auth = AuthService(Settings(app=AppSection(default_target="demo_pg"), targets={}),
                       pg_session_factory, directory_factory=factory, clock=clock)
    await auth.add_directory("corp", "entra", settings(), SECRET, actor="setup")
    assert (await auth.probe_directory("corp", actor="setup"))["status"] == "verified"
    await auth.sync_directory("corp", actor="setup")
    await auth.grant("user", actor="setup", role=("corp", "GAIA-Finance"))
    await auth.grant("admin", actor="setup", role=("corp", "GAIA-Admins"))
    saved = dict(vars(app.state)["_state"])
    app.state.auth = auth
    yield app, auth, fake, clock, pg_session_factory
    app.state._state.clear()
    app.state._state.update(saved)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


async def _go(client: httpx.AsyncClient, fake: FakeMicrosoft, oid: str):
    """The whole round trip: start, the user at Microsoft, the callback."""
    r = await client.get("/api/auth/oidc/start", params={"directory": "corp"})
    assert r.status_code == 302
    code, state = fake.authorize(r.headers["location"], oid)
    return await client.get("/api/auth/oidc/callback", params={"code": code, "state": state}), \
        code, state


async def _reasons(factory) -> list[str]:
    async with factory() as s:
        return [e.detail["reason"] for e in (await s.scalars(sa.select(AuthEvent).where(
            AuthEvent.action == "sign_in_refused").order_by(AuthEvent.seq))).all()]


@pytest.mark.asyncio
async def test_signing_in_with_microsoft(corp):
    app, _auth, fake, _clock, factory = corp
    async with _client(app) as client:
        r = await client.get("/api/auth/options")
        assert {"name": "corp", "kind": "entra"} in r.json()["directories"]
        r = await client.get("/api/auth/oidc/start", params={"directory": "corp"})
        location = urlsplit(r.headers["location"])
        assert location.netloc == "login.microsoftonline.com"
        binding = next(c for c in r.headers.get_list("set-cookie") if c.startswith("gaia_oidc="))
        assert "HttpOnly" in binding and "samesite=lax" in binding.lower()
        assert "Path=/api/auth/oidc" in binding
        code, state = fake.authorize(r.headers["location"], "a1")
        r = await client.get("/api/auth/oidc/callback", params={"code": code, "state": state})
        # a page that moves on by itself: the Strict session cookie rides on that navigation
        assert r.status_code == 200 and 'http-equiv="refresh"' in r.text and "url=/" in r.text
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
        me = (await client.get("/api/auth/me")).json()
        assert me["login"] == "ayse@corp.example" and me["directory"] == "corp"
        assert me["roles"] == ["user"]
        # an administrator through an Entra group reaches the admin API
    async with _client(app) as admin:
        r, _c, _s = await _go(admin, fake, "d4")
        assert r.status_code == 200
        assert (await admin.get("/api/admin/accounts")).status_code == 200
    async with factory() as s:
        event = await s.scalar(sa.select(AuthEvent).where(AuthEvent.action == "sign_in")
                               .order_by(AuthEvent.seq))
        assert event.detail["method"] == "oidc" and event.actor == "ayse@corp.example"
        rows = (await s.scalars(sa.select(PendingLogin))).all()
        assert rows and all(p.used_at for p in rows)  # single use
        # the PKCE verifier is stored encrypted (a 43-character token would be 43 bytes)
        assert all(len(bytes(p.verifier)) > 43 + 12 and p.verifier_key_id for p in rows)


@pytest.mark.asyncio
async def test_what_is_refused(corp):
    app, auth, fake, clock, factory = corp
    async with _client(app) as client:
        # a replay of a completed callback
        r, code, state = await _go(client, fake, "a1")
        assert r.status_code == 200
        r = await client.get("/api/auth/oidc/callback", params={"code": code, "state": state})
        assert r.status_code == 401 and "signin=failed" in r.text
    # the callback arriving in another browser (no binding cookie): login CSRF
    async with _client(app) as victim, _client(app) as attacker:
        r = await attacker.get("/api/auth/oidc/start", params={"directory": "corp"})
        code, state = fake.authorize(r.headers["location"], "a1")
        r = await victim.get("/api/auth/oidc/callback", params={"code": code, "state": state})
        assert r.status_code == 401
        assert (await victim.get("/api/auth/me")).status_code == 401
    async with _client(app) as client:
        # too slow: the attempt expired
        r = await client.get("/api/auth/oidc/start", params={"directory": "corp"})
        code, state = fake.authorize(r.headers["location"], "a1")
        clock.advance(minutes=11)
        r = await client.get("/api/auth/oidc/callback", params={"code": code, "state": state})
        assert r.status_code == 401
        # Microsoft said no (the user cancelled, a policy refused)
        r = await client.get("/api/auth/oidc/start", params={"directory": "corp"})
        _code, state = fake.authorize(r.headers["location"], "a1")
        r = await client.get("/api/auth/oidc/callback",
                             params={"error": "access_denied", "state": state})
        assert r.status_code == 401
        # no Gaia role; disabled at Microsoft
        for oid in ("b2", "c3"):
            r, _c, _s = await _go(client, fake, oid)
            assert r.status_code == 401
        # a made-up state
        r = await client.get("/api/auth/oidc/callback", params={"code": "x", "state": "y"})
        assert r.status_code == 401
    assert await _reasons(factory) == [
        "this sign-in was already completed (a replay)",
        "the sign-in came back to another browser",
        "the sign-in took too long (expired)",
        "Microsoft refused: access_denied",
        "no Gaia role (no grant to the account or its directory roles)",
        "disabled in the directory",
        "unknown or already used sign-in state",
    ]
    async with factory() as s:
        actors = [e.actor for e in (await s.scalars(sa.select(AuthEvent).where(
            AuthEvent.action == "sign_in_refused").order_by(AuthEvent.seq))).all()]
    assert actors[4:6] == ["berk@corp.example", "cem@corp.example"]
    # a password never goes to an Entra directory
    with pytest.raises(SignInRefused):
        await auth.sign_in("ayse@corp.example", "anything-at-all", directory="corp")


@pytest.mark.asyncio
async def test_an_open_session_follows_microsoft(corp):
    app, _auth, fake, clock, _factory = corp
    async with _client(app) as client:
        r, _c, _s = await _go(client, fake, "a1")
        assert (await client.get("/api/auth/me")).status_code == 200
        fake.groups["g-fin"]["members"].remove("a1")  # no longer in GAIA-Finance
        clock.advance(minutes=16)
        r = await client.get("/api/auth/me")
        assert r.status_code == 401 and "no Gaia role" in r.json()["detail"]
        fake.groups["g-fin"]["members"].append("a1")
        r, _c, _s = await _go(client, fake, "a1")
        fake.users["a1"]["accountEnabled"] = False  # disabled at Microsoft
        clock.advance(minutes=16)
        assert (await client.get("/api/auth/me")).status_code == 401
