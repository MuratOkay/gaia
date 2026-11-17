"""Phase 5A over HTTP, on the real application (``gaia.main.app``): sign-in, cookies,
CSRF, who may call what, conversations that belong to their owner, the administrator's
grants and the auditor's reports (D68–D70)."""

from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from _fake_directory import PASSWORDS  # noqa: E402
from test_auth_pg import Clock, Net, _ready  # noqa: E402

from gaia.api.chat import ChatService
from gaia.auth.service import AuthService
from gaia.core.config import AppSection, Settings
from gaia.datasources.crypto import new_key
from gaia.db.models.audit import AuthEvent


@pytest.fixture()
async def api(pg_session_factory, monkeypatch):
    monkeypatch.setenv("GAIA_MASTER_KEY", new_key())
    from gaia.main import app

    settings = Settings(app=AppSection(default_target="demo_pg"), targets={})
    net = Net()
    auth = AuthService(settings, pg_session_factory, directory_factory=net.factory,
                       clock=Clock())
    await _ready(auth, pg_session_factory)
    await auth.grant("admin", actor="setup", role=("acme", "GAIA-Admins"))
    await auth.grant("user", actor="setup", role=("acme", "GAIA-Finance"))
    await auth.grant("sme", actor="setup", account=("acme", "akaya"), data_source="demo_pg")
    await auth.grant("auditor", actor="setup", role=("acme", "GAIA-HR"))
    saved = dict(vars(app.state)["_state"])
    app.state.auth = auth
    app.state.chat = ChatService(settings, session_factory=pg_session_factory,
                                 llm_factory=lambda: None, use_vectors=False)
    yield app, auth, pg_session_factory
    app.state._state.clear()
    app.state._state.update(saved)


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gaia")


async def _login(client, login, password, **extra) -> dict:
    r = await client.post("/api/auth/login", json={"login": login, "password": password, **extra})
    assert r.status_code == 200, r.text
    return r.json()


MATRIX = [  # (method, path, who may: set of logins)
    ("GET", "/api/auth/me", {"akaya", "dyilmaz", "bdemir"}),
    ("GET", "/api/data-sources", {"akaya", "dyilmaz"}),  # chat roles: admin, sme (until 5C)
    ("GET", "/api/conversations?data_source=demo_pg", {"akaya", "dyilmaz"}),
    ("GET", "/api/admin/accounts", {"dyilmaz"}),
    ("GET", "/api/admin/data-sources", {"dyilmaz"}),
    ("GET", "/api/admin/grants", {"dyilmaz"}),
    ("GET", "/api/meta", {"dyilmaz"}),
    ("GET", "/api/review", {"akaya"}),  # an SME (5B); never the administrator
    ("GET", "/api/review/demo_pg/queue", {"akaya"}),
    ("GET", "/api/audit/summary", {"bdemir"}),
    ("GET", "/api/audit/access", {"bdemir"}),
    ("GET", "/api/audit/events", {"bdemir"}),
]


@pytest.mark.asyncio
async def test_who_may_call_what(api):
    app, _auth, factory = api
    async with await _client(app) as anonymous:
        for _method, path, _who in MATRIX:
            assert (await anonymous.get(path)).status_code == 401, path
        assert (await anonymous.get("/api/auth/options")).json()["directories"] == [
            {"name": "acme", "kind": "ldap"}]
        r = await anonymous.post("/api/auth/login", json={"login": "akaya", "password": "no"})
        assert r.status_code == 401 and r.json()["detail"] == "sign-in failed"
    for login in ("akaya", "dyilmaz", "bdemir"):
        async with await _client(app) as client:
            await _login(client, login, PASSWORDS[login])
            for _method, path, allowed in MATRIX:
                code = (await client.get(path)).status_code
                assert code == (200 if login in allowed else 403), (login, path, code)
    async with factory() as s:
        refused = (await s.scalars(sa.select(AuthEvent).where(
            AuthEvent.action == "forbidden"))).all()
    assert {(e.actor, e.subject) for e in refused} >= {
        ("akaya", "GET /api/admin/accounts"), ("bdemir", "GET /api/data-sources"),
        ("dyilmaz", "GET /api/audit/summary")}


@pytest.mark.asyncio
async def test_cookies_csrf_and_sign_out(api):
    app, _auth, _factory = api
    async with await _client(app) as client:
        r = await client.post("/api/auth/login",
                              json={"login": "akaya", "password": PASSWORDS["akaya"]})
        cookies = r.headers.get_list("set-cookie")
        session = next(c for c in cookies if c.startswith("gaia_session="))
        csrf = next(c for c in cookies if c.startswith("gaia_csrf="))
        assert "HttpOnly" in session and "samesite=strict" in session.lower()
        assert "HttpOnly" not in csrf and "samesite=strict" in csrf.lower()
        assert PASSWORDS["akaya"] not in r.text
        me = (await client.get("/api/auth/me")).json()
        assert me["roles"] == ["user", "sme"] and me["sme_sources"]
        # changing something without the CSRF header is refused, with it allowed
        assert (await client.post("/api/auth/logout")).status_code == 401
        token = client.cookies.get("gaia_csrf")
        assert (await client.post("/api/auth/logout",
                                  headers={"X-CSRF-Token": token})).status_code == 200
        assert (await client.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_a_conversation_belongs_to_whoever_started_it(api):
    app, _auth, _factory = api
    async with await _client(app) as ayse, await _client(app) as deniz:
        await _login(ayse, "akaya", PASSWORDS["akaya"])
        await _login(deniz, "dyilmaz", PASSWORDS["dyilmaz"])
        h = {"X-CSRF-Token": ayse.cookies.get("gaia_csrf")}
        r = await ayse.post("/api/conversations", json={"data_source": "demo_pg"}, headers=h)
        assert r.status_code == 201
        cid = r.json()["id"]
        assert [c["id"] for c in (await ayse.get(
            "/api/conversations?data_source=demo_pg")).json()] == [cid]
        assert (await deniz.get("/api/conversations?data_source=demo_pg")).json() == []
        assert (await deniz.get(f"/api/conversations/{cid}")).status_code == 404
        r = await deniz.post(f"/api/conversations/{cid}/messages", json={"content": "hi"},
                             headers={"X-CSRF-Token": deniz.cookies.get("gaia_csrf")})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_the_administrator_grants_and_the_auditor_reads(api):
    app, auth, _factory = api
    async with await _client(app) as admin, await _client(app) as auditor:
        await _login(admin, "dyilmaz", PASSWORDS["dyilmaz"])
        await _login(auditor, "bdemir", PASSWORDS["bdemir"])
        h = {"X-CSRF-Token": admin.cookies.get("gaia_csrf")}
        roles = (await admin.get("/api/admin/directories/acme/roles")).json()
        finance = next(r for r in roles if r["name"] == "GAIA-Finance")
        r = await admin.post("/api/admin/grants", headers=h, json={
            "privilege": "pii_processor", "directory_role_id": finance["id"],
            "note": "KVKK processors list, 2026-11"})
        assert r.status_code == 201 and r.json()["subject"] == "role acme/GAIA-Finance"
        r = await admin.post("/api/admin/grants", headers=h, json={
            "privilege": "auditor", "login": "dyilmaz", "directory": "acme"})
        assert r.status_code == 201  # granted — and not applied: an admin cannot audit
        r = await admin.post("/api/admin/grants", headers=h, json={"privilege": "root",
                                                                   "login": "akaya"})
        assert r.status_code == 400
        # the auditor's view of it
        access = {a["login"]: a for a in (await auditor.get("/api/audit/access")).json()}
        assert access["akaya"]["pii_processor"] and access["dyilmaz"]["conflicts"]
        assert access["dyilmaz"]["roles"] == ["user", "admin"]
        pii = (await auditor.get("/api/audit/pii-processors")).json()
        assert {p["login"] for p in pii} == {"akaya", "cpasif", "dyilmaz", "e*star"}
        summary = (await auditor.get("/api/audit/summary")).json()
        assert summary["chain"]["ok"] and len(summary["chain"]["head"]) == 64
        assert any("dyilmaz" in c for c in summary["conflicts"])
        csv = await auditor.get("/api/audit/grants?format=csv")
        assert csv.headers["content-type"].startswith("text/csv")
        assert "pii_processor" in csv.text and "KVKK processors list" in csv.text
        events = (await auditor.get("/api/audit/events?action=grant_added")).json()
        assert {e["actor"] for e in events} >= {"dyilmaz", "setup"}
        # revoking is dated and signed, never deleted
        gid = next(g["id"] for g in (await admin.get("/api/admin/grants")).json()
                   if g["privilege"] == "pii_processor")
        assert (await admin.delete(f"/api/admin/grants/{gid}", headers=h)).status_code == 200
        history = (await auditor.get("/api/audit/grants")).json()
        revoked = next(g for g in history if g["id"] == gid)
        assert revoked["revoked_by"] == "dyilmaz" and revoked["revoked_at"]


@pytest.mark.asyncio
async def test_the_local_account_is_set_up_with_a_one_time_code(api):
    app, auth, _factory = api
    code = await auth.issue_setup_code(actor="cli:root")
    async with await _client(app) as client:
        assert (await client.get("/api/auth/options")).json()["local_setup_pending"] is True
        r = await client.post("/api/auth/setup", json={"code": "nope", "password": "x" * 12})
        assert r.status_code == 400
        r = await client.post("/api/auth/setup",
                              json={"code": code, "password": "Local-Admin-Pass-1"})
        assert r.status_code == 200
        me = await _login(client, "gaia-admin", "Local-Admin-Pass-1")
        assert me["local"] and me["roles"] == ["admin"]
        assert (await client.get("/api/admin/local-account")).json()["password_set"] is True
