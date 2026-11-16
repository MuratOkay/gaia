"""Managing data sources (Phase 4F, OUTLINE §24; the CLI and the admin API use this).

Life of an admin-added data source::

    add ──> unverified ──probe──> verified ──(any change)──> unverified
                │                    │  ▲
                └──probe fails──> failed ┘ (probe again)
    disable / enable (back to unverified) · remove ──> tombstoned (secret wiped)

Rules (OUTLINE §24 "Non-negotiables"):

* the password is encrypted before it is stored (``crypto``) and never returned,
  logged or put in an error or an audit record; ``view`` is all that leaves;
* a data source is usable only when its read-only probe passed (``verified``) — the
  same 9-check probe as ``make check-target``, which proves read-only from privileges
  and by a write that must fail; any change makes it ``unverified`` again;
* a name is unique across config and store: an admin row never shadows a target of
  ``config/gaia.yaml`` (config targets stay the bootstrap path);
* removal tombstones: the row and everything that points at it stay, the secret is
  wiped, the name is freed (``name~<id>``);
* every action is an ``audit.connection_change`` — which fields changed, never values.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.core.config import Settings, TargetConfig
from gaia.datasources.crypto import decrypt, encrypt
from gaia.datasources.resolver import clear_cache, target_from
from gaia.db.models.audit import ConnectionChange
from gaia.db.models.catalog import DataSource

__all__ = ["DataSourceError", "DataSourceService", "DataSourceSpec"]

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_DIALECTS = {"mssql": "mssql", "postgres": "postgres", "postgresql": "postgres"}
_SETTINGS = ("dialect", "host", "port", "database", "username", "options", "limits",
             "capabilities", "profiling_enabled", "profiling_window")


class DataSourceError(ValueError):
    """A request the rules refuse (the message is safe to show)."""


class DataSourceSpec(BaseModel):
    """The non-secret settings of a data source; the password travels separately."""

    name: str
    dialect: str
    host: str
    port: int = Field(ge=1, le=65535)
    database: str
    username: str
    options: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    profiling_enabled: bool = True
    profiling_window: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("a name is lower case letters, digits and _, starting with a "
                             "letter (2–63 characters)")
        return v

    @field_validator("dialect")
    @classmethod
    def _dialect(cls, v: str) -> str:
        if v.lower() not in _DIALECTS:
            raise ValueError(f"dialect must be one of {', '.join(sorted(_DIALECTS))}")
        return _DIALECTS[v.lower()]

    def connection(self) -> dict[str, Any]:
        """What is stored in ``catalog.data_sources.connection``; validated as a target."""
        doc = self.model_dump(exclude={"name"})
        TargetConfig.model_validate({**doc, "password": ""})  # options/limits are checked
        return doc


def _sanitise(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


class DataSourceService:
    def __init__(self, session: AsyncSession, settings: Settings, actor: str | None = None):
        self.session = session
        self.settings = settings
        self.actor = actor

    # -- reading ----------------------------------------------------------------------------

    async def _row(self, name: str) -> DataSource:
        row = await self.session.scalar(select(DataSource).where(DataSource.name == name))
        if row is None:
            raise DataSourceError(f"no data source {name!r}")
        return row

    async def _admin_row(self, name: str) -> DataSource:
        row = await self._row(name)
        if row.origin != "admin":
            raise DataSourceError(f"{name!r} comes from config/gaia.yaml: change it there")
        if row.status == "tombstoned":
            raise DataSourceError(f"{name!r} was removed")
        return row

    @staticmethod
    def view(row: DataSource | None, *, name: str | None = None,
             config: TargetConfig | None = None) -> dict[str, Any]:
        """A data source as it may be shown: never the password or the secret."""
        if row is None and config is not None:  # configured, never introspected
            return {"name": name, "origin": "config", "status": "config",
                    "dialect": config.dialect, "host": config.host, "port": config.port,
                    "database": config.database, "username": config.username,
                    "has_secret": False, "probed_at": None, "probe": None,
                    "introspected": False}
        assert row is not None
        conn = row.connection or {}
        if row.origin == "config" and config is not None:
            conn = {"host": config.host, "port": config.port, "database": config.database,
                    "username": config.username}
        return {"name": row.name, "origin": row.origin, "status": row.status,
                "dialect": row.dialect, "host": conn.get("host"), "port": conn.get("port"),
                "database": conn.get("database") or row.database_name,
                "username": conn.get("username"), "has_secret": row.secret is not None,
                "probed_at": row.probed_at, "probe": row.probe,
                "introspected": row.server_version is not None or row.origin == "config",
                "created_at": row.created_at, "updated_at": row.updated_at}

    async def list(self, include_removed: bool = False) -> list[dict[str, Any]]:
        rows = {r.name: r for r in await self.session.scalars(select(DataSource))}
        out = [self.view(rows.get(n), name=n, config=t) for n, t in
               sorted(self.settings.targets.items())]
        for name, row in sorted(rows.items()):
            if name in self.settings.targets:
                continue
            if row.status == "tombstoned" and not include_removed:
                continue
            out.append(self.view(row))
        return out

    def target(self, row: DataSource) -> TargetConfig:
        """The full target of an admin row, password decrypted (for the probe)."""
        if row.secret is None or row.secret_key_id is None:
            raise DataSourceError(f"{row.name!r} has no stored credential")
        return target_from(row.connection or {},
                           decrypt(bytes(row.secret), row.secret_key_id, row.id))

    # -- changing ----------------------------------------------------------------------------

    async def _out(self, row: DataSource) -> dict[str, Any]:
        """Flush, reload what the database set (timestamps), forget cached targets."""
        await self.session.flush()
        await self.session.refresh(row)
        clear_cache()
        return self.view(row)

    async def _audit(self, row: DataSource, action: str, **detail: Any) -> None:
        self.session.add(ConnectionChange(data_source_id=row.id, data_source_name=row.name,
                                          action=action, actor=self.actor, detail=detail))

    async def add(self, spec: DataSourceSpec, password: str) -> dict[str, Any]:
        if spec.name in self.settings.targets:
            raise DataSourceError(f"{spec.name!r} is a target of config/gaia.yaml; choose "
                                  "another name")
        existing = await self.session.scalar(select(DataSource).where(
            DataSource.name == spec.name))
        if existing is not None:
            raise DataSourceError(f"a data source {spec.name!r} exists")
        if not password:
            raise DataSourceError("a password is required")
        connection = spec.connection()
        row = DataSource(id=uuid.uuid4(), name=spec.name, dialect=spec.dialect,
                         database_name=spec.database, origin="admin", status="unverified",
                         connection=connection)
        row.secret, row.secret_key_id = encrypt(password, row.id)
        self.session.add(row)
        await self.session.flush()
        await self._audit(row, "created", fields=sorted(connection))
        return await self._out(row)

    async def probe(self, name: str) -> dict[str, Any]:
        """Run the dialect's read-only probe; ``verified`` only when every check passes."""
        from gaia.dialects import bootstrap  # noqa: F401 (registers the probes)
        from gaia.dialects.registry import registry

        row = await self._admin_row(name)
        if row.status == "disabled":
            raise DataSourceError(f"{name!r} is disabled: enable it first")
        target = self.target(row)
        checks = await asyncio.to_thread(registry.probe(target.dialect), target)
        results = [{"name": c.name, "ok": bool(c.ok),
                    "detail": _sanitise(str(c.detail), target.password)} for c in checks]
        ok = bool(results) and all(c["ok"] for c in results)
        row.status = "verified" if ok else "failed"
        row.probe = results
        row.probed_at = datetime.now(UTC)
        await self._audit(row, "probed", verified=ok,
                          failed=[c["name"] for c in results if not c["ok"]])
        return await self._out(row)

    async def update(self, name: str, changes: dict[str, Any],
                     password: str | None = None) -> dict[str, Any]:
        row = await self._admin_row(name)
        unknown = sorted(set(changes) - set(_SETTINGS))
        if unknown:
            raise DataSourceError(f"cannot change {', '.join(unknown)}")
        current = DataSourceSpec.model_validate({"name": row.name, **(row.connection or {})})
        spec = current.model_copy(update=changes)
        spec = DataSourceSpec.model_validate(spec.model_dump())  # re-validate
        connection = spec.connection()
        changed = sorted(k for k in connection if connection[k] != (row.connection or {}).get(k))
        row.connection, row.dialect, row.database_name = connection, spec.dialect, spec.database
        if password:
            row.secret, row.secret_key_id = encrypt(password, row.id)
            changed.append("password")
        if changed:
            row.status = "unverified" if row.status != "disabled" else "disabled"
            row.probe, row.probed_at = None, None
            await self._audit(row, "updated", fields=changed)
        return await self._out(row)

    async def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        row = await self._admin_row(name)
        row.status = "unverified" if enabled else "disabled"
        if enabled:
            row.probe, row.probed_at = None, None
        await self._audit(row, "enabled" if enabled else "disabled")
        return await self._out(row)

    async def remove(self, name: str) -> dict[str, Any]:
        row = await self._admin_row(name)
        await self._audit(row, "removed")
        row.status = "tombstoned"
        row.secret, row.secret_key_id = None, None
        row.tombstoned_at = datetime.now(UTC)
        row.name = f"{name}~{str(row.id)[:8]}"
        return await self._out(row)

    async def rotate(self) -> int:
        """Re-encrypt every stored secret with the current master key."""
        rows = await self.session.scalars(select(DataSource).where(
            DataSource.secret.is_not(None)))
        n = 0
        for row in rows:
            plain = decrypt(bytes(row.secret), row.secret_key_id, row.id)
            row.secret, new_id = encrypt(plain, row.id)
            if new_id != row.secret_key_id:
                row.secret_key_id = new_id
                await self._audit(row, "rotated", key_id=new_id)
                n += 1
        await self.session.flush()
        clear_cache()
        return n
