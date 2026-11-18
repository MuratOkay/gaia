"""Models as managed things (Phase 7D; P7-4, P7-5; D101).

A customer changes the model, or the provider — in production. Everything Gaia measures is
already per model id (meanings, golden runs, labels, calibrators), so history survives a
change; what this module adds is the change as an **event**:

* **Registered beside the old model, not in its place.** The model ``config/gaia.yaml`` names
  (``llm:``) is always there, read-only (``origin: config``). Others are registered at run time
  (``make model ARGS="add …"``, the *Models* page): a URL, the id the API is sent, a key — the
  name of an environment variable holding it, or the key itself encrypted with
  ``GAIA_MASTER_KEY`` as a data source's password is (4F) —, a locality (D24: a ``remote`` model
  sees data values only masked) and options (JSON mode, thinking, concurrency, prices). A new
  model is **probed** (plain, JSON mode, thinking — ``make check-llm``'s checks) before it may
  serve anything.
* **Two purposes.** *reasoning* (the meanings of columns, with their critique) and *answering*
  (SQL, repairs, narration; golden runs and drafts). Each is served by the configured model
  until a **switch** assigns another; every tool, job and the chat read the model of their
  purpose here (``effective_settings``), so nothing else has to know.
* **A switch needs a trial** (``gaia.models.trial``) of that model for that purpose no older
  than ``models.trial_valid_days`` — or ``force``, recorded. The model replaced stays ready:
  ``rollback`` returns to it while ``models.rollback_days`` run. Stored meanings are not touched
  by a switch: the nightly refresh re-reasons them with the new model under its token budget,
  a part each night (the skip key sees the model changed), SME decisions kept (§24d).
* **A new model starts uncalibrated.** Calibrators are per model id; none passes from one model
  to another except through a declared rename (``calibration.model_aliases``, P6-9).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.core.config import LLMConfig, Settings
from gaia.db.models.ops import MODEL_PURPOSES, Job, Model, ModelAssignment

__all__ = [
    "ModelEntry", "ModelError", "add_model", "assignments", "current", "effective_llm",
    "effective_settings", "get_model", "list_models", "llm_config_of", "meanings_by_model",
    "probe_model",
    "retire", "rollback", "settings_of", "switch",
]

_OPTIONS = {"json_mode", "max_output_tokens", "temperature", "concurrency", "timeout_s",
            "thinking", "context_budget_tokens", "price_in_per_million",
            "price_out_per_million"}


class ModelError(ValueError):
    """A request the rules refuse (the message is safe to show)."""


@dataclass
class ModelEntry:
    name: str
    origin: str  # config | admin
    base_url: str
    model_id: str
    locality: str
    status: str  # config | unverified | verified | failed | retired
    options: dict[str, Any] = field(default_factory=dict)
    key_env: str | None = None
    has_key: bool = False
    probe: list[Any] | None = None
    probed_at: datetime | None = None
    created_by: str | None = None
    row: Model | None = None

    @property
    def usable(self) -> bool:
        return self.status in ("config", "verified")

    def view(self) -> dict[str, Any]:
        return {"name": self.name, "origin": self.origin, "base_url": self.base_url,
                "model_id": self.model_id, "locality": self.locality, "status": self.status,
                "options": self.options, "key_env": self.key_env, "has_key": self.has_key,
                "probe": self.probe, "probed_at": self.probed_at,
                "created_by": self.created_by}


def _config_entry(settings: Settings) -> ModelEntry:
    cfg = settings.llm
    return ModelEntry(name=cfg.model, origin="config", base_url=cfg.base_url,
                      model_id=cfg.model, locality=cfg.locality, status="config",
                      options={"json_mode": cfg.json_mode, "concurrency": cfg.concurrency},
                      has_key=bool(cfg.api_key))


def _entry(row: Model) -> ModelEntry:
    return ModelEntry(name=row.name, origin="admin", base_url=row.base_url,
                      model_id=row.model_id, locality=row.locality, status=row.status,
                      options=dict(row.options or {}), key_env=row.key_env,
                      has_key=bool(row.secret) or bool(row.key_env and os.environ.get(
                          row.key_env)) or (row.locality == "local" and not row.key_env),
                      probe=row.probe, probed_at=row.probed_at,
                      created_by=row.created_by, row=row)


async def list_models(session: AsyncSession, settings: Settings, *,
                      include_retired: bool = False) -> list[ModelEntry]:
    rows = (await session.scalars(select(Model).order_by(Model.created_at))).all()
    out = [_config_entry(settings)]
    out += [_entry(r) for r in rows if include_retired or r.status != "retired"]
    return out


async def get_model(session: AsyncSession, settings: Settings, name: str) -> ModelEntry:
    if name == settings.llm.model:
        return _config_entry(settings)
    row = await session.scalar(select(Model).where(Model.name == name))
    if row is None:
        raise ModelError(f"no model {name!r} (make model ARGS=\"list\")")
    return _entry(row)


def llm_config_of(entry: ModelEntry, settings: Settings) -> LLMConfig:
    """The ``llm`` settings that reach ``entry``: the configuration's, with the entry's URL,
    id, key, locality and options over them."""
    if entry.origin == "config":
        return settings.llm
    key = ""
    if entry.key_env:
        key = os.environ.get(entry.key_env, "")
    elif entry.row is not None and entry.row.secret:
        from gaia.datasources.crypto import decrypt

        key = decrypt(bytes(entry.row.secret), entry.row.secret_key_id or "", entry.row.id)
    elif entry.locality == "local":
        key = "local"  # a local server (Ollama's /v1) asks for none; the client sends one
    update: dict[str, Any] = {"provider": "deepseek", "base_url": entry.base_url,
                              "model": entry.model_id, "api_key": key,
                              "locality": entry.locality, "replay_dir": None}
    for k, v in entry.options.items():
        if k == "thinking" and isinstance(v, dict):
            update["thinking"] = settings.llm.thinking.model_copy(update=v)
        elif k in _OPTIONS and k not in ("price_in_per_million", "price_out_per_million"):
            update[k] = v
    return settings.llm.model_copy(update=update)


async def assignments(session: AsyncSession) -> dict[str, ModelAssignment]:
    rows = (await session.scalars(select(ModelAssignment).where(
        ModelAssignment.until.is_(None)))).all()
    return {r.purpose: r for r in rows}


async def current(session: AsyncSession, settings: Settings, purpose: str) -> ModelEntry:
    """The model serving ``purpose`` now (the configured one unless switched)."""
    if purpose not in MODEL_PURPOSES:
        raise ModelError(f"purpose is one of {', '.join(MODEL_PURPOSES)}")
    row = (await assignments(session)).get(purpose)
    if row is None:
        return _config_entry(settings)
    return await get_model(session, settings, row.model_name)


async def effective_llm(session: AsyncSession, settings: Settings, purpose: str) -> LLMConfig:
    """The ``llm`` settings of the model serving ``purpose`` now (one indexed read: the chat
    asks on every turn, so a switch is seen at the next question)."""
    try:
        return llm_config_of(await current(session, settings, purpose), settings)
    except Exception as exc:  # a store before migration 0023: the configured model
        if "model_assignment" not in str(exc):
            raise
        await session.rollback()
        return settings.llm


async def effective_settings(session: AsyncSession, settings: Settings,
                             purpose: str) -> Settings:
    """``settings`` with ``llm`` the model that serves ``purpose`` — what every tool, job and
    the chat pass on, so that nothing else reads the registry."""
    cfg = await effective_llm(session, settings, purpose)
    return settings if cfg is settings.llm else settings.model_copy(update={"llm": cfg})


async def settings_of(settings: Settings, purpose: str) -> Settings:
    """``effective_settings`` with a session of its own (the command-line tools)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from gaia.db.engine import get_engine

    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as session:
        return await effective_settings(session, settings, purpose)


async def meanings_by_model(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Per data source, its current column meanings by the model that reasoned them (the SME's
    decisions apart, under ``"SME-decided"``: a switch keeps them) — how far a re-reasoning
    after a switch has come."""
    from sqlalchemy import func

    from gaia.db.models.catalog import DataSource, TableRecord
    from gaia.db.models.semantics import ColumnSemantic

    decided = ColumnSemantic.reviewed_by.is_not(None)
    rows = (await session.execute(
        select(DataSource.name, ColumnSemantic.model, decided, func.count())
        .join(TableRecord, TableRecord.id == ColumnSemantic.table_id)
        .join(DataSource, DataSource.id == TableRecord.data_source_id)
        .where(ColumnSemantic.is_current.is_(True), TableRecord.is_tombstoned.is_(False))
        .group_by(DataSource.name, ColumnSemantic.model, decided))).all()
    out: dict[str, dict[str, int]] = {}
    for ds, model, is_decided, n in rows:
        key = "SME-decided" if is_decided else model
        out.setdefault(ds, {})
        out[ds][key] = out[ds].get(key, 0) + n
    return {ds: dict(sorted(m.items(), key=lambda kv: -kv[1])) for ds, m in sorted(out.items())}


def clear_cache() -> None:
    """Nothing is cached (kept for callers that clear after a change)."""


async def add_model(session: AsyncSession, settings: Settings, *, name: str, base_url: str,
                    model_id: str, by: str, key: str | None = None,
                    key_env: str | None = None, locality: str = "remote",
                    options: dict[str, Any] | None = None) -> ModelEntry:
    name = (name or "").strip()
    if not name or len(name) > 80 or any(c.isspace() for c in name):
        raise ModelError("a name is 1–80 characters without spaces")
    if name == settings.llm.model:
        raise ModelError(f"{name!r} is the configured model (config/gaia.yaml)")
    if await session.scalar(select(Model.id).where(Model.name == name)) is not None:
        raise ModelError(f"a model {name!r} is registered already")
    if locality not in ("local", "remote"):
        raise ModelError("locality is local or remote (D24)")
    if key and key_env:
        raise ModelError("give the key (stored encrypted) or the name of the environment "
                         "variable that holds it — not both")
    if not key and not key_env and locality != "local":
        raise ModelError("a remote model needs a key: give it (stored encrypted) or the name "
                         "of the environment variable that holds it")
    unknown = set(options or {}) - _OPTIONS
    if unknown:
        raise ModelError(f"unknown option(s): {', '.join(sorted(unknown))} "
                         f"({', '.join(sorted(_OPTIONS))})")
    row = Model(name=name, base_url=base_url.strip(), model_id=model_id.strip(),
                key_env=key_env or None, locality=locality, options=dict(options or {}),
                status="unverified", created_by=by)
    session.add(row)
    await session.flush()
    if key:
        from gaia.datasources.crypto import encrypt

        row.secret, row.secret_key_id = encrypt(key, row.id)
    await session.flush()
    return _entry(row)


PROBE_JSON = ('Respond with json only, exactly this shape: {"business_name": "<short label>", '
              '"purpose": "<one sentence>", "confidence": <0..1>}')


def run_probe(client: Any) -> list[dict[str, Any]]:
    """``make check-llm``'s three checks on any client: a plain answer, JSON mode, thinking."""
    from gaia.llm.base import Message

    checks: list[dict[str, Any]] = []

    def check(name: str, work) -> None:
        started = time.perf_counter()
        try:
            detail = work()
            checks.append({"name": name, "ok": True, "detail": detail,
                           "seconds": round(time.perf_counter() - started, 2)})
        except Exception as exc:  # noqa: BLE001 - each check says why it failed
            checks.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: "
                           f"{str(exc)[:200]}", "seconds": round(time.perf_counter() - started,
                                                                  2)})

    def plain() -> str:
        r = client.complete([Message("user", "Reply with the single word: ready")],
                            thinking=False, max_tokens=32, temperature=0.0)
        if not r.content.strip():
            raise ValueError("blank content")
        return f"{r.content.strip()[:40]!r} from {r.model}"

    def json_mode() -> str:
        r = client.complete([Message("system", PROBE_JSON), Message(
            "user", "Column: dbo.Orders.OrderTotal, decimal(12,2), comment 'Gross order "
                    "amount'.")], json_mode=True, thinking=False, max_tokens=256,
            temperature=0.0)
        doc = json.loads(r.content)
        if not isinstance(doc, dict) or "business_name" not in doc:
            raise ValueError(f"not the shape asked: {r.content[:80]!r}")
        return f"business_name={doc['business_name']!r}"

    def thinking() -> str:
        r = client.complete([Message("system", PROBE_JSON), Message(
            "user", "Column: hr.Employees.Salary, money, nullable. Its business meaning?")],
            json_mode=True, thinking=True, max_tokens=2048)
        json.loads(r.content)
        return f"reasoning {len(r.reasoning or '')} chars" + (
            "" if (r.reasoning or "").strip() else " (none returned: thinking may be off)")

    check("plain", plain)
    if not checks[0]["ok"]:  # nothing answers: the other checks would only wait as long again
        checks += [{"name": n, "ok": False, "detail": "not tried: the plain answer failed",
                    "seconds": 0} for n in ("json", "thinking")]
        return checks
    check("json", json_mode)
    check("thinking", thinking)
    return checks


async def probe_model(session: AsyncSession, settings: Settings, name: str, *,
                      client_factory=None) -> ModelEntry:
    """Probe a registered model: ``verified`` when the plain answer and JSON mode work
    (thinking is reported, not required — a model without it serves with it off)."""
    entry = await get_model(session, settings, name)
    if entry.origin == "config":
        raise ModelError("the configured model is checked with `make check-llm`")
    if entry.status == "retired":
        raise ModelError(f"{name} is retired")
    cfg = llm_config_of(entry, settings)
    # a probe is asked, and answered, at once: few retries, a short wait (2 minutes a call)
    cfg = cfg.model_copy(update={"timeout_s": min(cfg.timeout_s, 120), "retry": cfg.retry
                                 .model_copy(update={"max_attempts": 2,
                                                     "max_attempts_rate_limit": 3,
                                                     "max_attempts_timeout": 1})})
    if not cfg.api_key:
        checks = [{"name": "key", "ok": False, "detail": (
            f"the environment variable {entry.key_env} is not set" if entry.key_env
            else "no key"), "seconds": 0}]
    else:
        if client_factory is not None:
            client = client_factory(cfg)
        else:
            from gaia.llm.deepseek import build_client

            client = build_client(cfg)
        try:
            import asyncio

            checks = await asyncio.to_thread(run_probe, client)
        finally:
            close = getattr(client, "close", None)
            if close:
                close()
    ok = all(c["ok"] for c in checks if c["name"] in ("plain", "json", "key"))
    row = entry.row
    assert row is not None
    row.probe, row.probed_at = checks, datetime.now(UTC)
    row.status = "verified" if ok else "failed"
    await session.flush()
    clear_cache()
    return _entry(row)


async def _last_trial(session: AsyncSession, name: str, purpose: str,
                      since: datetime) -> Job | None:
    rows = (await session.scalars(select(Job).where(
        Job.kind == "model_trial", Job.state == "done", Job.finished_at >= since)
        .order_by(Job.finished_at.desc()))).all()
    for j in rows:
        p = j.params or {}
        if p.get("model") == name and p.get("purpose", "both") in (purpose, "both"):
            return j
    return None


async def switch(session: AsyncSession, settings: Settings, purpose: str, name: str, *,
                 by: str, force: bool = False, note: str | None = None,
                 now: datetime | None = None) -> ModelAssignment:
    """Let ``name`` serve ``purpose`` from now on. Needs the model usable and — unless
    ``force`` — a finished trial of it for this purpose no older than
    ``models.trial_valid_days``."""
    now = now or datetime.now(UTC)
    entry = await get_model(session, settings, name)
    if not entry.usable:
        raise ModelError(f"{name} is {entry.status}: probe it first (make model ARGS=\"probe "
                         f"{name}\")")
    before = await current(session, settings, purpose)
    if before.name == name:
        raise ModelError(f"{name} serves {purpose} already")
    trial = await _last_trial(session, name, purpose,
                              now - timedelta(days=settings.models.trial_valid_days))
    if trial is None and not force and entry.origin != "config":
        raise ModelError(f"no trial of {name} for {purpose} in the last "
                         f"{settings.models.trial_valid_days} days: `make model ARGS=\"trial "
                         f"{name} --purpose {purpose}\"` (or force, which is recorded)")
    old = (await assignments(session)).get(purpose)
    if old is not None:
        old.until = now
        await session.flush()
    row = ModelAssignment(purpose=purpose, model_name=name, previous=before.name, since=now,
                          rollback_until=now + timedelta(days=settings.models.rollback_days),
                          trial_job_id=trial.id if trial else None,
                          forced=bool(force and trial is None and entry.origin != "config"),
                          by=by, note=note)
    session.add(row)
    await session.flush()
    clear_cache()
    return row


async def rollback(session: AsyncSession, settings: Settings, purpose: str, *, by: str,
                   note: str | None = None, now: datetime | None = None) -> ModelAssignment:
    """Back to the model the current one replaced — while ``models.rollback_days`` run (and it
    is still registered). No trial is needed: it served this purpose before."""
    now = now or datetime.now(UTC)
    cur = (await assignments(session)).get(purpose)
    if cur is None or not cur.previous:
        raise ModelError(f"nothing to roll back: {purpose} is served by the configured model "
                         "and never was switched")
    if cur.rollback_until and now > cur.rollback_until:
        raise ModelError(f"the rollback period of {purpose} ended on "
                         f"{cur.rollback_until:%Y-%m-%d}: switch (with a trial) instead")
    back = await get_model(session, settings, cur.previous)
    if not back.usable:
        raise ModelError(f"{back.name} is {back.status}: it can no longer serve")
    cur.until = now
    await session.flush()
    row = ModelAssignment(purpose=purpose, model_name=back.name, previous=cur.model_name,
                          since=now, rollback_until=None, forced=False, by=by,
                          note=note or f"rollback from {cur.model_name}")
    session.add(row)
    await session.flush()
    clear_cache()
    return row


async def retire(session: AsyncSession, settings: Settings, name: str, *,
                 by: str) -> ModelEntry:
    """No longer offered. Refused while it serves a purpose, or is what a current switch may
    roll back to. Its meanings, runs and labels stay, read-only, as they are."""
    entry = await get_model(session, settings, name)
    if entry.origin == "config":
        raise ModelError("the configured model is changed in config/gaia.yaml")
    now = datetime.now(UTC)
    for purpose, row in (await assignments(session)).items():
        if row.model_name == name:
            raise ModelError(f"{name} serves {purpose}: switch first")
        if row.previous == name and row.rollback_until and now <= row.rollback_until:
            raise ModelError(f"{purpose} may still roll back to {name} until "
                             f"{row.rollback_until:%Y-%m-%d}")
    assert entry.row is not None
    entry.row.status, entry.row.retired_at = "retired", now
    entry.row.secret = None  # the key is wiped, as a removed data source's password is
    await session.flush()
    clear_cache()
    return _entry(entry.row)

