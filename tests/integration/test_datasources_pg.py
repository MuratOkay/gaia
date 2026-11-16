"""Phase 4F on ``demo_pg``: a data source added at runtime, from onboarding to removal.

The target is the sandbox's ``demo_pg``, added a second time under other names: once
with the read-only role (the probe passes), once with the superuser (the probe must
fail). The resolver reads the disposable metadata store, so ``GAIA_METADATA_URL``
points there for the test.

Credentials differ by host: the sandbox's roles trust any password, while a real host
authenticates the read-only role with the one in ``GAIA_TEST_PG_TARGET_URL`` and the
superuser with ``GAIA_PG_TARGET_ADMIN_PASSWORD`` (``.env``)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

import httpx
import pytest
import sqlalchemy as sa
from dotenv import load_dotenv
from fastapi import FastAPI

from gaia.core.config import ENV_PATH
from gaia.datasources.crypto import new_key
from gaia.datasources.resolver import TargetUnusable, clear_cache
from gaia.datasources.service import DataSourceError, DataSourceService, DataSourceSpec
from gaia.db.models.audit import ConnectionChange
from gaia.db.models.catalog import DataSource

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
DB = os.environ.get("GAIA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (URL and DB), reason="needs both PostgreSQL databases")


def _read_only_password() -> str:
    """The read-only role's password from ``GAIA_TEST_PG_TARGET_URL``.

    A password-authenticated host rejects anything else; the sandbox trusts any
    password, and a short/absent one is replaced so it cannot occur by accident.
    """
    value = unquote(urlsplit(URL).password or "")
    return value if len(value) >= 8 else "gaia-Test-Secret-7319"


def _admin_password(fallback: str) -> str:
    """The superuser's password: ``GAIA_PG_TARGET_ADMIN_PASSWORD`` from ``.env``.

    On the sandbox the roles trust any password, so the read-only one is a fine
    fallback there.
    """
    load_dotenv(ENV_PATH, override=False)
    return os.environ.get("GAIA_PG_TARGET_ADMIN_PASSWORD") or fallback


def _spec(name: str, user: str) -> DataSourceSpec:
    parts = urlsplit(URL)
    return DataSourceSpec(name=name, dialect="postgres", host=parts.hostname or "127.0.0.1",
                          port=parts.port or 5432, database=parts.path.lstrip("/"),
                          username=user, limits={"statement_timeout_s": 20})


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("GAIA_METADATA_URL", DB)
    monkeypatch.setenv("GAIA_MASTER_KEY", new_key())
    monkeypatch.delenv("GAIA_MASTER_KEY_PREVIOUS", raising=False)
    clear_cache()
    yield
    clear_cache()


@pytest.mark.asyncio
async def test_a_data_source_from_onboarding_to_removal(pg_session_factory, env, monkeypatch):
    from test_profiling_pg import _settings

    from gaia.db.repositories.snapshot_store import SnapshotStore
    from gaia.dialects.postgres.catalog import PostgresCatalogReader
    from gaia.dialects.postgres.guard import PostgresGuard
    from gaia.query.catalog import GuardCatalog
    from gaia.query.executor import execute

    factory = pg_session_factory
    settings = _settings()
    # Real on a password-authenticated host; on the trust-authenticated sandbox any
    # password works, so this one is deliberately unlike the target's name.
    password = _read_only_password()

    async with factory() as s:
        svc = DataSourceService(s, settings, actor="tester")
        # a config target's name cannot be taken
        with pytest.raises(DataSourceError, match="config/gaia.yaml"):
            await svc.add(_spec("demo_pg", "gaia_ro"), password)
        view = await svc.add(_spec("shop_ro", "gaia_ro"), password)
        await s.commit()
    assert view["status"] == "unverified" and "password" not in json.dumps(view, default=str)
    async with factory() as s:
        row = await s.scalar(sa.select(DataSource).where(DataSource.name == "shop_ro"))
        assert row.secret and password.encode() not in bytes(row.secret)
        assert row.connection["username"] == "gaia_ro" and "password" not in row.connection

    # unusable until proven read-only
    with pytest.raises(TargetUnusable, match="unverified"):
        settings.target("shop_ro")
    async with factory() as s:
        view = await DataSourceService(s, settings).probe("shop_ro")
        await s.commit()
    assert view["status"] == "verified", view["probe"]
    assert len(view["probe"]) == 9 and all(c["ok"] for c in view["probe"])
    target = settings.target("shop_ro")
    assert target.username == "gaia_ro" and target.limits.statement_timeout_s == 20

    # every tool now works with it: introspect, then a guarded query
    snapshot = PostgresCatalogReader(target).read_snapshot(data_source="shop_ro")
    async with factory() as s:
        await SnapshotStore(s).apply(snapshot, data_source_name="shop_ro")
        await s.commit()
        row = await s.scalar(sa.select(DataSource).where(DataSource.name == "shop_ro"))
        assert row.origin == "admin" and row.server_version  # the same row, now introspected
    guard = PostgresGuard(GuardCatalog.from_snapshot(snapshot), row_limit=5)
    result = execute(settings.target("shop_ro"),
                     guard.check("SELECT count(*) AS n FROM sales.orders"), row_limit=5)
    assert result.ok and result.rows[0][0] == 250_000

    # a superuser is not read-only: the probe fails, the data source stays unusable
    async with factory() as s:
        svc = DataSourceService(s, settings)
        await svc.add(_spec("shop_admin", "postgres"), _admin_password(password))
        view = await svc.probe("shop_admin")
        await s.commit()
    assert view["status"] == "failed"
    assert "security.privileges" in {c["name"] for c in view["probe"] if not c["ok"]}
    with pytest.raises(TargetUnusable, match="failed"):
        settings.target("shop_admin")

    # any change must be proven again; so must an old proof
    async with factory() as s:
        view = await DataSourceService(s, settings).update("shop_ro", {
            "limits": {"statement_timeout_s": 25}})
        await s.commit()
    assert view["status"] == "unverified"
    with pytest.raises(TargetUnusable):
        settings.target("shop_ro")
    async with factory() as s:
        await DataSourceService(s, settings).probe("shop_ro")
        await s.commit()
    assert settings.target("shop_ro").limits.statement_timeout_s == 25
    async with factory() as s:
        await s.execute(sa.update(DataSource).where(DataSource.name == "shop_ro").values(
            probed_at=datetime.now(UTC) - timedelta(days=settings.datasources.reprobe_days + 1)))
        await s.commit()
    clear_cache()
    with pytest.raises(TargetUnusable, match="more than 30 days ago"):
        settings.target("shop_ro")
    async with factory() as s:
        await DataSourceService(s, settings).probe("shop_ro")
        await s.commit()

    # a new master key: the stored credential moves to it
    old = os.environ["GAIA_MASTER_KEY"]
    monkeypatch.setenv("GAIA_MASTER_KEY", new_key())
    monkeypatch.setenv("GAIA_MASTER_KEY_PREVIOUS", old)
    async with factory() as s:
        moved = await DataSourceService(s, settings).rotate()
        await s.commit()
    assert moved == 2
    monkeypatch.delenv("GAIA_MASTER_KEY_PREVIOUS")
    clear_cache()
    assert settings.target("shop_ro").username == "gaia_ro"  # the old key is no longer needed

    # removal: tombstoned, credential gone, name free, history kept
    async with factory() as s:
        ds_id = (await s.scalar(sa.select(DataSource).where(DataSource.name == "shop_ro"))).id
        view = await DataSourceService(s, settings).remove("shop_ro")
        await s.commit()
    assert view["status"] == "tombstoned" and view["name"].startswith("shop_ro~")
    with pytest.raises(KeyError):
        settings.target("shop_ro")
    async with factory() as s:
        row = await s.get(DataSource, ds_id)
        assert row.secret is None and row.tombstoned_at is not None
        snapshots = await s.scalar(sa.text(
            "SELECT count(*) FROM catalog.snapshots WHERE data_source_id = :i"), {"i": ds_id})
        assert snapshots == 1
        await DataSourceService(s, settings).add(_spec("shop_ro", "gaia_ro"), "x")  # free
        await s.commit()
        changes = (await s.scalars(sa.select(ConnectionChange).order_by(
            ConnectionChange.created_at))).all()
    actions = [(c.data_source_name, c.action) for c in changes]
    assert actions[:4] == [("shop_ro", "created"), ("shop_ro", "probed"),
                           ("shop_admin", "created"), ("shop_admin", "probed")]
    assert ("shop_ro", "updated") in actions and ("shop_ro", "removed") in actions
    assert sum(1 for a in actions if a[1] == "rotated") == 2
    assert changes[0].actor == "tester"
    every = json.dumps([c.detail for c in changes])
    assert password not in every

    # the listing: config targets and stored ones, never a credential
    async with factory() as s:
        views = await DataSourceService(s, settings).list()
    by = {v["name"]: v for v in views}
    assert by["demo_pg"]["origin"] == "config" and by["shop_admin"]["status"] == "failed"
    assert not any("shop_ro~" in n for n in by)
    assert all("secret" not in v and "secret_key_id" not in v for v in views)
    assert password not in json.dumps(views, default=str)


@pytest.mark.asyncio
async def test_the_admin_api_is_off_until_switched_on(pg_session_factory, env):
    from test_profiling_pg import _introspect, _settings

    from gaia.api.admin import router as admin_router
    from gaia.api.chat import ChatService
    from gaia.api.chat import router as chat_router

    await _introspect(pg_session_factory)
    settings = _settings()
    # The real read-only password: a password-authenticated host refuses a placeholder,
    # and it must still never be echoed back by the API.
    password = _read_only_password()
    app = FastAPI()
    app.include_router(chat_router)
    app.include_router(admin_router)
    app.state.chat = ChatService(settings, session_factory=pg_session_factory,
                                 llm_factory=lambda: None, use_vectors=False)
    spec = _spec("api_ro", "gaia_ro").model_dump() | {"password": password}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://gaia") as client:
        assert (await client.get("/api/admin/data-sources")).status_code == 404
        settings.datasources.admin_api = True
        r = await client.post("/api/admin/data-sources", json=spec)
        assert r.status_code == 201 and password not in r.text
        r = await client.post("/api/admin/data-sources/api_ro/probe")
        assert r.status_code == 200 and r.json()["status"] == "verified"
        r = await client.post("/api/admin/data-sources", json=spec)
        assert r.status_code == 400 and "exists" in r.json()["detail"]
        r = await client.patch("/api/admin/data-sources/demo_pg", json={"host": "x"})
        assert r.status_code == 400 and "config/gaia.yaml" in r.json()["detail"]
        listing = (await client.get("/api/admin/data-sources")).text
        assert password not in listing and '"api_ro"' in listing
        sources = {d["name"]: d for d in (await client.get("/api/data-sources")).json()}
        assert sources["api_ro"]["status"] == "verified"
        assert sources["api_ro"]["usable"] is False  # verified, but not introspected yet
        assert sources["demo_pg"]["usable"] is True
        r = await client.delete("/api/admin/data-sources/api_ro")
        assert r.status_code == 200 and r.json()["status"] == "tombstoned"
