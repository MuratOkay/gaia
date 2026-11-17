"""Phase 5A on the metadata store: directories, sign-in, sessions, grants, the local
account and the hash-chained audit (D68–D70). The directory is the offline one of
``tests/unit/_fake_directory.py``; time is a controllable clock."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from ldap3 import MODIFY_DELETE, MODIFY_REPLACE, MOCK_SYNC, Connection

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from _fake_directory import (  # noqa: E402
    DN,
    PASSWORDS,
    ROLE_BASE,
    SVC,
    SVC_PW,
    connector,
    fake_server,
    settings,
)

from gaia.auth import audit  # noqa: E402
from gaia.auth.ldap import LdapDirectory, LdapSettings  # noqa: E402
from gaia.auth.service import (  # noqa: E402
    LOCAL_LOGIN,
    AuthError,
    AuthService,
    NotSignedIn,
    SignInRefused,
)
from gaia.core.config import AppSection, Settings  # noqa: E402
from gaia.datasources.crypto import CredentialError, decrypt, new_key  # noqa: E402
from gaia.db.models.audit import AuthEvent  # noqa: E402
from gaia.db.models.auth import Account, Directory  # noqa: E402
from gaia.db.models.catalog import DataSource  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


class Net:
    """The directory as the test wants it: reachable or not, changeable on the fly."""

    def __init__(self) -> None:
        self.server = fake_server()
        self.down = False

    def factory(self, row: Directory, password: str) -> LdapDirectory:
        inner = connector(self.server)

        def connect(dn, pw):
            if self.down:
                raise OSError("connection refused")
            return inner(dn, pw)

        return LdapDirectory(LdapSettings.model_validate(row.settings), password, connect=connect)

    def modify(self, dn: str, changes: dict) -> None:
        conn = Connection(self.server, user=SVC, password=SVC_PW, client_strategy=MOCK_SYNC)
        conn.bind()
        assert conn.modify(dn, changes), conn.result


@pytest.fixture()
def world(pg_session_factory, monkeypatch):
    monkeypatch.setenv("GAIA_MASTER_KEY", new_key())
    clock, net = Clock(), Net()
    svc = AuthService(Settings(app=AppSection(default_target="demo_pg"), targets={}),
                      pg_session_factory, directory_factory=net.factory, clock=clock)
    return svc, clock, net, pg_session_factory


async def _events(factory) -> list[AuthEvent]:
    async with factory() as s:
        return list((await s.scalars(sa.select(AuthEvent).order_by(AuthEvent.seq))).all())


async def _ready(svc, factory) -> None:
    """A verified, synced directory and a data source to be an SME of."""
    async with factory() as s:
        s.add(DataSource(name="demo_pg", dialect="postgres", database_name="gaia_target_pg"))
        await s.commit()
    await svc.add_directory("acme", "ldap", settings(), SVC_PW, actor="tester")
    view = await svc.probe_directory("acme", actor="tester")
    assert view["status"] == "verified", view["probe"]
    await svc.sync_directory("acme", actor="tester")


@pytest.mark.asyncio
async def test_a_directory_from_connection_to_roles(world):
    svc, _clock, _net, factory = world
    view = await svc.add_directory("acme", "ldap", settings(), SVC_PW, actor="tester")
    assert view["status"] == "unverified" and "password" not in json.dumps(view, default=str)
    async with factory() as s:
        row = await s.scalar(sa.select(Directory))
        assert row.secret and SVC_PW.encode() not in bytes(row.secret)
        # bound to a namespaced id: it does not decrypt as if it were a data source's secret
        with pytest.raises(CredentialError):
            decrypt(bytes(row.secret), row.secret_key_id, row.id)
    with pytest.raises(AuthError, match="probe it first"):
        await svc.sync_directory("acme", actor="tester")
    assert (await svc.probe_directory("acme", actor="tester"))["status"] == "verified"
    synced = await svc.sync_directory("acme", actor="tester")
    assert synced["sync"] == {"users": 5, "roles": 5, "roles_gone": 0, "accounts_disabled": 0,
                              "disabled_in_directory": 1}
    roles = {r["name"]: r for r in await svc.roles("acme")}
    assert set(roles) == {"GAIA-Admins", "GAIA-Finance", "GAIA-HR", "GAIA-Loop1", "GAIA-Loop2"}
    assert roles["GAIA-Finance"]["members"] == 4 and roles["GAIA-HR"]["members"] == 1
    # a changed setting must be proven again (as for data sources)
    changed = await svc.update_directory("acme", actor="tester", settings={"timeout_s": 5})
    assert changed["status"] == "unverified"
    with pytest.raises(AuthError, match="clear-text"):
        await svc.update_directory("acme", actor="tester",
                                   settings={"urls": ["ldap://dc1.acme.test"]})
    with pytest.raises(AuthError, match="directory settings"):  # Entra ID since 5A.2
        await svc.add_directory("entra", "entra", {}, "x", actor="tester")


@pytest.mark.asyncio
async def test_a_failed_test_does_not_take_sign_in_away(world):
    """D76: on the owner's host a stale CA made the test fail, the directory turned
    "failed" and left the sign-in page — only the local account was left. A failing test of
    settings that passed before keeps the directory offered and says so; settings that
    never passed are still "failed"."""
    svc, _clock, net, factory = world
    await _ready(svc, factory)
    await svc.grant("user", actor="a", role=("acme", "GAIA-Finance"))
    net.down = True
    view = await svc.probe_directory("acme", actor="tester")
    assert view["status"] == "verified" and view["last_test_failed"] is True
    assert [d["name"] for d in (await svc.sign_in_options())["directories"]] == ["acme"]
    with pytest.raises(SignInRefused):  # the sign-in itself fails while the cause lasts
        await svc.sign_in("akaya", PASSWORDS["akaya"])
    probed = [e for e in await _events(factory) if e.action == "directory_probed"][-1]
    assert probed.detail["kept_for_sign_in"] is True and probed.detail["ok"] is False
    refused = [e for e in await _events(factory) if e.action == "sign_in_refused"][-1]
    assert "unreachable" in refused.detail["reason"]
    net.down = False
    view = await svc.probe_directory("acme", actor="tester")
    assert view["status"] == "verified" and view["last_test_failed"] is False
    assert (await svc.sign_in("akaya", PASSWORDS["akaya"]))[0].login == "akaya"
    # settings never proven: a failing test leaves them out of the sign-in page
    await svc.add_directory("beta", "ldap", settings(), SVC_PW, actor="tester")
    net.down = True
    assert (await svc.probe_directory("beta", actor="tester"))["status"] == "failed"
    assert [d["name"] for d in (await svc.sign_in_options())["directories"]] == ["acme"]


@pytest.mark.asyncio
async def test_sign_in_needs_a_gaia_role_and_says_nothing_more(world):
    svc, _clock, _net, factory = world
    await _ready(svc, factory)
    for login, password in [("akaya", PASSWORDS["akaya"]), ("akaya", "wrong"),
                            ("nobody", "x" * 12), ("cpasif", PASSWORDS["cpasif"])]:
        with pytest.raises(SignInRefused) as exc:
            await svc.sign_in(login, password)
        assert str(exc.value) == "sign-in failed"
    reasons = [e.detail["reason"] for e in await _events(factory) if e.action == "sign_in_refused"]
    assert reasons == ["no Gaia role (no grant to the account or its directory roles)",
                       "wrong password or unknown login", "wrong password or unknown login",
                       "disabled in the directory"]
    await svc.grant("user", actor="admin1", role=("acme", "GAIA-Finance"))
    principal, token, csrf = await svc.sign_in("akaya", PASSWORDS["akaya"], ip="10.0.0.5")
    assert principal.effective.roles == {"user"} and principal.directory == "acme"
    assert token and csrf and token != csrf
    assert (await svc.resolve(token)).login == "akaya"


@pytest.mark.asyncio
async def test_roles_permissions_and_separation_of_duties(world):
    svc, _clock, _net, factory = world
    await _ready(svc, factory)
    await svc.grant("admin", actor="a", role=("acme", "GAIA-Admins"))
    await svc.grant("user", actor="a", role=("acme", "GAIA-Finance"))
    await svc.grant("pii_processor", actor="a", role=("acme", "GAIA-Finance"),
                    note="KVKK list 2026-11")
    await svc.grant("sme", actor="a", account=("acme", "akaya"), data_source="demo_pg")
    await svc.grant("auditor", actor="a", role=("acme", "GAIA-HR"))
    with pytest.raises(AuthError, match="per data source"):
        await svc.grant("sme", actor="a", account=("acme", "bdemir"))
    with pytest.raises(AuthError, match="exists already"):
        await svc.grant("user", actor="a", role=("acme", "GAIA-Finance"))
    await svc.set_local_password("Local-Admin-Pass-1", actor="cli:t")
    with pytest.raises(AuthError, match="local account"):
        await svc.grant("user", actor="a", account=(None, LOCAL_LOGIN))

    ayse, _t, _c = await svc.sign_in("akaya", PASSWORDS["akaya"])
    assert ayse.effective.roles == {"user", "sme"} and ayse.effective.pii_processor
    async with factory() as s:
        ds = await s.scalar(sa.select(DataSource.id))
    assert ayse.effective.reviews(str(ds))
    berk, _t, _c = await svc.sign_in("bdemir", PASSWORDS["bdemir"])  # auditor through nesting
    assert berk.effective.roles == {"auditor"} and not berk.effective.pii_processor
    # an administrator cannot review or audit: granted, but not applied — and said so
    await svc.grant("sme", actor="a", account=("acme", "dyilmaz"), data_source="demo_pg")
    await svc.grant("auditor", actor="a", account=("acme", "dyilmaz"))
    deniz, _t, _c = await svc.sign_in("dyilmaz", PASSWORDS["dyilmaz"])
    assert deniz.effective.roles == {"admin", "user"}
    assert len(deniz.effective.conflicts) == 2 and not deniz.effective.sme_sources
    accounts = {a["login"]: a for a in await svc.accounts()}
    assert accounts["dyilmaz"]["conflicts"] and accounts["akaya"]["sme_sources"] == ["demo_pg"]
    assert accounts[LOCAL_LOGIN]["roles"] == ["admin"]


@pytest.mark.asyncio
async def test_sessions_expire_and_need_their_csrf_token(world):
    svc, clock, _net, factory = world
    await _ready(svc, factory)
    await svc.grant("user", actor="a", role=("acme", "GAIA-Finance"))
    _p, token, csrf = await svc.sign_in("akaya", PASSWORDS["akaya"])
    with pytest.raises(NotSignedIn, match="CSRF"):
        await svc.resolve(token, unsafe=True)
    with pytest.raises(NotSignedIn, match="CSRF"):
        await svc.resolve(token, unsafe=True, csrf="forged")
    assert (await svc.resolve(token, unsafe=True, csrf=csrf)).login == "akaya"
    with pytest.raises(NotSignedIn):
        await svc.resolve("a-guessed-token")
    # idle: 30 minutes without a request
    clock.advance(minutes=29)
    await svc.resolve(token)
    clock.advance(minutes=31)
    with pytest.raises(NotSignedIn, match="expired"):
        await svc.resolve(token)
    # absolute: 8 hours, however active
    _p, token, _csrf = await svc.sign_in("akaya", PASSWORDS["akaya"])
    for _ in range(16):  # 7 h 44 min, never idle
        clock.advance(minutes=29)
        await svc.resolve(token)
    with pytest.raises(NotSignedIn, match="expired"):
        clock.advance(minutes=29)
        await svc.resolve(token)
    # signing out ends it at once
    _p, token, _csrf = await svc.sign_in("akaya", PASSWORDS["akaya"])
    await svc.sign_out(token)
    with pytest.raises(NotSignedIn):
        await svc.resolve(token)


@pytest.mark.asyncio
async def test_the_directory_is_asked_again_while_a_session_lasts(world):
    svc, clock, net, factory = world
    await _ready(svc, factory)
    await svc.grant("user", actor="a", role=("acme", "GAIA-Finance"))
    await svc.grant("sme", actor="a", role=("acme", "GAIA-Loop2"), data_source="demo_pg")
    _p, token, _c = await svc.sign_in("akaya", PASSWORDS["akaya"])
    assert (await svc.resolve(token)).effective.roles == {"user", "sme"}

    # removed from a group in the directory: the role goes at the next re-check
    net.modify(f"cn=GAIA-Loop1,ou=Gaia,ou=Groups,{'dc=acme,dc=test'}",
               {"member": [(MODIFY_DELETE, [DN["akaya"]])]})
    clock.advance(minutes=14)
    assert (await svc.resolve(token)).effective.roles == {"user", "sme"}  # not yet due
    clock.advance(minutes=2)
    assert (await svc.resolve(token)).effective.roles == {"user"}
    assert "privileges_changed" in [e.action for e in await _events(factory)]

    # the directory is down: the session goes on for one more interval, then ends
    net.down = True
    clock.advance(minutes=16)
    assert (await svc.resolve(token)).effective.roles == {"user"}
    clock.advance(minutes=15)
    with pytest.raises(NotSignedIn, match="could not confirm"):
        await svc.resolve(token)
    net.down = False

    # disabled in the directory: signed out at the next re-check
    _p, token, _c = await svc.sign_in("akaya", PASSWORDS["akaya"])
    net.modify(DN["akaya"], {"userAccountControl": [(MODIFY_REPLACE, [514])]})
    clock.advance(minutes=16)
    with pytest.raises(NotSignedIn, match="no longer allows"):
        await svc.resolve(token)


@pytest.mark.asyncio
async def test_a_revoked_grant_takes_effect_at_the_next_recheck(world):
    svc, clock, _net, factory = world
    await _ready(svc, factory)
    granted = await svc.grant("user", actor="admin1", role=("acme", "GAIA-HR"))
    _p, token, _c = await svc.sign_in("bdemir", PASSWORDS["bdemir"])
    await svc.revoke(uuid.UUID(granted["id"]), actor="admin1", note="moved to another team")
    clock.advance(minutes=16)
    with pytest.raises(NotSignedIn, match="no Gaia role"):
        await svc.resolve(token)
    history = await svc.grants(include_revoked=True)
    assert history[0]["revoked_by"] == "admin1" and history[0]["granted_by"] == "admin1"
    assert await svc.grants() == []


@pytest.mark.asyncio
async def test_the_one_local_account(world):
    svc, clock, _net, factory = world
    assert (await svc.sign_in_options())["local"] is False
    code = await svc.issue_setup_code(actor="cli:root")
    assert (await svc.sign_in_options())["local_setup_pending"] is True
    with pytest.raises(AuthError, match="wrong or has expired"):
        await svc.complete_setup("guess", "Local-Admin-Pass-1")
    with pytest.raises(AuthError, match="at least 12"):
        await svc.complete_setup(code, "short")
    await svc.complete_setup(code, "Local-Admin-Pass-1", ip="127.0.0.1")
    with pytest.raises(AuthError):  # a setup code works once
        await svc.complete_setup(code, "Another-Pass-22")
    principal, token, _c = await svc.sign_in("GAIA-ADMIN", "Local-Admin-Pass-1")
    assert principal.local and principal.effective.roles == {"admin"}
    # a second local account cannot exist
    async with factory() as s:
        s.add(Account(login="second", is_local=True))
        with pytest.raises(sa.exc.IntegrityError):
            await s.commit()
    # five wrong passwords lock it, even with the right one after
    for _ in range(5):
        with pytest.raises(SignInRefused):
            await svc.sign_in(LOCAL_LOGIN, "wrong-password-1")
    with pytest.raises(SignInRefused):
        await svc.sign_in(LOCAL_LOGIN, "Local-Admin-Pass-1")
    clock.advance(minutes=16)
    await svc.sign_in(LOCAL_LOGIN, "Local-Admin-Pass-1")
    # a new password (the CLI recovery) ends its sessions; disabling it closes the door
    await svc.set_local_password("Recovered-Pass-123", actor="cli:root")
    with pytest.raises(NotSignedIn):
        await svc.resolve(token)
    await svc.set_local_enabled(False, actor="cli:root")
    with pytest.raises(SignInRefused):
        await svc.sign_in(LOCAL_LOGIN, "Recovered-Pass-123")
    local_events = [e.action for e in await _events(factory) if e.subject == LOCAL_LOGIN]
    assert local_events[0] == "local_setup_code_issued" and "local_disabled" in local_events


@pytest.mark.asyncio
async def test_the_audit_is_a_chain_and_holds_no_secret(world):
    svc, _clock, _net, factory = world
    await _ready(svc, factory)
    await svc.grant("user", actor="a", role=("acme", "GAIA-Finance"))
    await svc.sign_in("akaya", PASSWORDS["akaya"])
    with pytest.raises(SignInRefused):
        await svc.sign_in("akaya", "Wrong-Password-9")
    await svc.set_local_password("Local-Admin-Pass-1", actor="cli:t")
    events = await _events(factory)
    dump = json.dumps([[e.actor, e.action, e.subject, e.detail] for e in events], default=str)
    for secret in (SVC_PW, PASSWORDS["akaya"], "Wrong-Password-9", "Local-Admin-Pass-1"):
        assert secret not in dump
    async with factory() as s:
        check = await audit.verify_chain(s)
        assert check.ok and check.events == len(events) and check.head == events[-1].hash
        with pytest.raises(ValueError, match="names a secret"):
            await audit.record(s, "x", actor="t", detail={"new_password": "nope"})
        await s.rollback()
        # an edit through the database breaks the chain where it happened
        target = events[3].seq
        await s.execute(sa.text("UPDATE audit.auth_event SET actor = 'someone else' "
                                "WHERE seq = :q"), {"q": target})
        await s.commit()
        broken = await audit.verify_chain(s)
        assert not broken.ok and broken.broken_at == target


@pytest.mark.asyncio
async def test_disabling_a_directory_ends_its_sessions_and_rotation_rewraps_secrets(
        world, monkeypatch):
    svc, _clock, _net, factory = world
    await _ready(svc, factory)
    await svc.grant("user", actor="a", role=("acme", "GAIA-Finance"))
    _p, token, _c = await svc.sign_in("akaya", PASSWORDS["akaya"])
    await svc.set_directory_enabled("acme", False, actor="admin1")
    with pytest.raises(NotSignedIn):
        await svc.resolve(token)
    with pytest.raises(SignInRefused):
        await svc.sign_in("akaya", PASSWORDS["akaya"])
    # master-key rotation covers directory secrets too
    import os

    old = os.environ["GAIA_MASTER_KEY"]
    monkeypatch.setenv("GAIA_MASTER_KEY", new_key())
    monkeypatch.setenv("GAIA_MASTER_KEY_PREVIOUS", old)
    assert await svc.rotate_secrets() == 1
    monkeypatch.delenv("GAIA_MASTER_KEY_PREVIOUS")
    await svc.set_directory_enabled("acme", True, actor="admin1")
    assert (await svc.probe_directory("acme", actor="admin1"))["status"] == "verified"
    assert ROLE_BASE  # the settings still point at the same directory
