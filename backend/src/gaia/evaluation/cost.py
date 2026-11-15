"""Criterion 5 of 3C: what the production pipeline costs, per table.

"Full run over all objects in the production mode (named, profiled): calls,
tokens, target pages and wall time reported per table." Read from what the
pipeline already stores: the latest reasoning of each table (``audit.llm_call``:
the run in which its current meaning was last produced by the model) and its
current profile (``profiling.table_profile``). Tables whose current version was
only re-scored or inherited are listed with the run that last asked the model.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gaia.core.config import Settings
from gaia.db.engine import dispose_engine, get_engine
from gaia.db.models.audit import LLMCall
from gaia.db.models.catalog import TableRecord
from gaia.db.models.profiling import TableProfile
from gaia.db.models.semantics import TableSemantic

__all__ = ["production_cost"]


async def production_cost(settings: Settings, label: str) -> str:
    from gaia.db.repositories.semantics_store import SemanticsStore

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            store = SemanticsStore(session)
            ds = await store.data_source(label)
            if ds is None:
                raise LookupError(f"no data source {label!r}")
            tables = {t.id: t for t in (await session.scalars(select(TableRecord).where(
                TableRecord.data_source_id == ds.id, TableRecord.is_tombstoned.is_(False))))}
            current = {s.table_id: s for s in (await session.scalars(select(TableSemantic).where(
                TableSemantic.table_id.in_(tables), TableSemantic.is_current.is_(True))))}
            profiles = {p.table_id: p for p in (await session.scalars(select(TableProfile).where(
                TableProfile.table_id.in_(tables), TableProfile.is_current.is_(True))))}
            # the latest run that called the model for each table
            latest = dict((await session.execute(
                select(LLMCall.table_id, func.max(LLMCall.created_at))
                .where(LLMCall.table_id.in_(tables), LLMCall.cache_hit.is_(False))
                .group_by(LLMCall.table_id))).all())
            calls: dict = {}
            for row in (await session.execute(select(LLMCall).where(
                    LLMCall.table_id.in_(latest), LLMCall.cache_hit.is_(False)))).scalars():
                calls.setdefault(row.table_id, {}).setdefault(row.run_id, []).append(row)
    finally:
        await dispose_engine()

    lines = [f"# Production run cost — {label}", "",
             "| Object | Kind | Rows (est.) | Calls | Tokens in | Tokens out | Model time s | "
             "Profile | Pages | Queries | Profile ms | Semantic version |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    totals = {"calls": 0, "tin": 0, "tout": 0, "ms": 0, "pages": 0, "queries": 0, "pms": 0}
    for tid, t in sorted(tables.items(), key=lambda kv: (kv[1].schema_name, kv[1].object_name)):
        runs = calls.get(tid, {})
        # the run holding the most recent call
        run = max(runs.values(), key=lambda rs: max(r.created_at for r in rs), default=[])
        tin = sum(r.prompt_tokens for r in run)
        tout = sum(r.completion_tokens for r in run)
        ms = sum(r.latency_ms for r in run)
        p = profiles.get(tid)
        sem = current.get(tid)
        totals["calls"] += len(run)
        totals["tin"] += tin
        totals["tout"] += tout
        totals["ms"] += ms
        if p is not None:
            totals["pages"] += p.pages_estimated
            totals["queries"] += p.queries
            totals["pms"] += p.runtime_ms
        lines.append(
            f"| {t.schema_name}.{t.object_name} | {t.kind} | {t.estimated_rows or 0:,} | "
            f"{len(run)} | {tin:,} | {tout:,} | {ms / 1000:.0f} | "
            f"{p.status if p else '—'} | {p.pages_estimated if p else 0:,} | "
            f"{p.queries if p else 0} | {p.runtime_ms if p else 0:,} | "
            f"{f'v{sem.version} ({sem.model})' if sem else 'not reasoned'} |")
    reasoned = sum(1 for tid in tables if tid in current)
    lines += ["", f"{len(tables)} object(s), {reasoned} with a meaning; model calls "
              f"{totals['calls']}, tokens {totals['tin']:,} in / {totals['tout']:,} out, "
              f"model time {totals['ms'] / 3600000:.1f} h (sum of call latency; wall time is "
              f"lower with concurrency {settings.llm.concurrency}); profiling "
              f"{totals['pages']:,} pages (estimated), {totals['queries']} queries, "
              f"{totals['pms'] / 1000:.0f} s."]
    return "\n".join(lines) + "\n"
