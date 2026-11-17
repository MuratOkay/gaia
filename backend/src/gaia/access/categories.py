"""Data categories (Phase 5C, D70, D74): defined by the administrator, proposed for each
object by Gaia, confirmed by an SME (what the data *is*), granted by the administrator
(who may *see* it).

**Gaia's proposal is deterministic** — no model decides anything about access. An object
is matched against each category's name and keywords: its schema (the strongest signal in
most databases), its name, and what Gaia understood it to be (business name, entity,
purpose). A view whose definition reads objects of one category only gets that category.
A tie, or no match, proposes nothing: the object stays readable by nobody until someone
decides. Proposals never replace a confirmed category.

**Suggestions for directory roles** work the same way over a role's name and description
("GAIA-Finance" → Finance). A suggestion grants nothing.

Every change is in the hash-chained audit (D70).
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.auth import audit
from gaia.db.models.auth import Category, Directory, DirectoryRole, Grant, ObjectCategory
from gaia.db.models.catalog import DataSource, TableRecord
from gaia.db.models.semantics import TableSemantic
from gaia.query.retrieval import tokens

__all__ = ["CategoryError", "CategoryService", "propose_for"]


class CategoryError(RuntimeError):
    """A request that cannot be done as asked (400)."""


def _vocabulary(c: Category) -> set[str]:
    words = set(tokens(c.name))
    for k in c.keywords or []:
        words |= set(tokens(k))
    return words


def propose_for(signals: list[tuple[str | None, float]], categories: list[Category]
                ) -> tuple[Category | None, str]:
    """The best category for weighted texts, and why; None on no match or a tie."""
    scores: Counter[uuid.UUID] = Counter()
    why: dict[uuid.UUID, list[str]] = {}
    vocab = {c.id: _vocabulary(c) for c in categories}
    for text, weight in signals:
        seen: set[tuple[uuid.UUID, str]] = set()
        for word in re.findall(r"[A-Za-z0-9]+", text or ""):  # the words as written
            for stem in tokens(word):
                for c in categories:
                    if stem in vocab[c.id] and (c.id, stem) not in seen:
                        seen.add((c.id, stem))
                        scores[c.id] += weight
                        why.setdefault(c.id, []).append(word.lower())
    if not scores:
        return None, "no category's name or keywords match"
    ranked = scores.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        names = sorted(c.name for c in categories if c.id in (ranked[0][0], ranked[1][0]))
        return None, f"a tie between {' and '.join(names)}"
    best = next(c for c in categories if c.id == ranked[0][0])
    return best, "matches " + ", ".join(sorted(set(why[best.id])))


class CategoryService:
    def __init__(self, session_factory, *, now=None) -> None:
        self.factory = session_factory
        self.now = now or (lambda: datetime.now(UTC))

    # -- categories -------------------------------------------------------------------------

    async def _category(self, s: AsyncSession, ref: str | uuid.UUID) -> Category:
        row = None
        try:
            row = await s.get(Category, uuid.UUID(str(ref)))
        except ValueError:
            row = await s.scalar(select(Category).where(
                func.lower(Category.name) == str(ref).strip().lower()))
        if row is None:
            raise CategoryError(f"no category {ref!r}")
        return row

    async def categories(self, *, include_retired: bool = False) -> list[dict[str, Any]]:
        async with self.factory() as s:
            q = select(Category).order_by(func.lower(Category.name))
            if not include_retired:
                q = q.where(Category.retired_at.is_(None))
            rows = (await s.scalars(q)).all()
            counts = dict((await s.execute(select(ObjectCategory.category_id, func.count())
                                           .where(ObjectCategory.is_current.is_(True))
                                           .group_by(ObjectCategory.category_id))).all())
            grants = dict((await s.execute(select(Grant.category_id, func.count()).where(
                Grant.privilege == "category", Grant.revoked_at.is_(None))
                .group_by(Grant.category_id))).all())
            return [_category_view(c, counts.get(c.id, 0), grants.get(c.id, 0)) for c in rows]

    async def add(self, name: str, *, actor: str, description: str | None = None,
                  keywords: list[str] | None = None) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise CategoryError("a category needs a name")
        async with self.factory() as s:
            if await s.scalar(select(Category.id).where(
                    func.lower(Category.name) == name.lower())):
                raise CategoryError(f"a category {name!r} exists already")
            c = Category(name=name, description=(description or "").strip() or None,
                         keywords=_clean(keywords), created_by=actor, created_at=self.now())
            s.add(c)
            await s.flush()
            await audit.record(s, "category_added", actor=actor, subject=name,
                               detail={"keywords": c.keywords, "description": c.description})
            await s.commit()
            return _category_view(c, 0, 0)

    async def change(self, ref: str, *, actor: str, name: str | None = None,
                     description: str | None = None, keywords: list[str] | None = None
                     ) -> dict[str, Any]:
        async with self.factory() as s:
            c = await self._category(s, ref)
            before = {"name": c.name, "description": c.description, "keywords": c.keywords}
            if name is not None and name.strip() and name.strip() != c.name:
                if await s.scalar(select(Category.id).where(
                        func.lower(Category.name) == name.strip().lower(), Category.id != c.id)):
                    raise CategoryError(f"a category {name.strip()!r} exists already")
                c.name = name.strip()
            if description is not None:
                c.description = description.strip() or None
            if keywords is not None:
                c.keywords = _clean(keywords)
            await audit.record(s, "category_changed", actor=actor, subject=c.name,
                               detail={"before": before})
            await s.commit()
            return _category_view(c, 0, 0)

    async def retire(self, ref: str, *, actor: str) -> dict[str, Any]:
        """A category no object is in any more stops being offered; its grants stay on
        record (and grant nothing)."""
        async with self.factory() as s:
            c = await self._category(s, ref)
            used = await s.scalar(select(func.count()).select_from(ObjectCategory).where(
                ObjectCategory.category_id == c.id, ObjectCategory.is_current.is_(True)))
            if used:
                raise CategoryError(f"{used} object(s) are in {c.name}: move them first")
            c.retired_at, c.retired_by = self.now(), actor
            await audit.record(s, "category_retired", actor=actor, subject=c.name)
            await s.commit()
            return _category_view(c, 0, 0)

    async def from_schemas(self, data_source: str, *, actor: str) -> list[dict[str, Any]]:
        """A starting point: one category per schema of the data source (its name the
        keyword), where none of that name exists yet."""
        async with self.factory() as s:
            ds = await _source(s, data_source)
            schemas = sorted(set((await s.scalars(select(TableRecord.schema_name).where(
                TableRecord.data_source_id == ds.id,
                TableRecord.is_tombstoned.is_(False)))).all()))
        out = []
        for schema in schemas:
            name = schema.replace("_", " ").strip().title() if schema.islower() else schema
            try:
                out.append(await self.add(name, actor=actor, keywords=[schema],
                                          description=f"the {schema} schema of {data_source}"))
            except CategoryError:
                continue
        return out

    # -- objects ----------------------------------------------------------------------------

    async def objects(self, data_source: str) -> list[dict[str, Any]]:
        """Every object of the data source with the category in force and who decided it."""
        async with self.factory() as s:
            ds = await _source(s, data_source)
            rows = (await s.execute(
                select(TableRecord, TableSemantic, ObjectCategory, Category)
                .outerjoin(TableSemantic, (TableSemantic.table_id == TableRecord.id)
                           & TableSemantic.is_current.is_(True))
                .outerjoin(ObjectCategory, (ObjectCategory.table_id == TableRecord.id)
                           & ObjectCategory.is_current.is_(True))
                .outerjoin(Category, Category.id == ObjectCategory.category_id)
                .where(TableRecord.data_source_id == ds.id,
                       TableRecord.is_tombstoned.is_(False))
                .order_by(TableRecord.schema_name, TableRecord.object_name))).all()
            return [_object_view(t, sem, oc, c) for t, sem, oc, c in rows]

    async def assign(self, data_source: str, table_id: uuid.UUID, category: str, *,
                     actor: str, source: str, note: str | None = None) -> dict[str, Any]:
        """An SME or an administrator says what an object's data is: confirmed."""
        if source not in ("sme", "admin"):
            raise CategoryError("only an SME or an administrator confirms a category")
        async with self.factory() as s:
            ds = await _source(s, data_source)
            table = await s.get(TableRecord, table_id)
            if table is None or table.data_source_id != ds.id:
                raise CategoryError("no such object in this data source")
            c = await self._category(s, category)
            if c.retired_at is not None:
                raise CategoryError(f"{c.name} is retired")
            before = await _current(s, table.id)
            await _replace(s, ds.id, table.id, c.id, status="confirmed", source=source,
                           actor=actor, reason=(note or "").strip() or None, at=self.now())
            await audit.record(s, "object_category_set", actor=actor,
                               subject=f"{ds.name}:{table.schema_name}.{table.object_name}",
                               detail={"category": c.name, "source": source,
                                       "before": str(before.category_id) if before else None,
                                       "note": note})
            await s.commit()
            return {"object": f"{table.schema_name}.{table.object_name}", "category": c.name,
                    "status": "confirmed", "source": source, "decided_by": actor}

    async def propose(self, data_source: str, *, actor: str = "gaia",
                      replace_proposals: bool = True) -> dict[str, Any]:
        """Gaia's proposal for every object without a confirmed category (tables first,
        then views from what they read)."""
        from gaia.access.policy import _tables_read
        from gaia.db.models.profiling import ModuleDefinition
        from gaia.profiling.lineage import sqlglot_dialect

        async with self.factory() as s:
            ds = await _source(s, data_source)
            cats = list((await s.scalars(select(Category).where(
                Category.retired_at.is_(None)))).all())
            if not cats:
                raise CategoryError("define categories first (or create them from the schemas)")
            rows = (await s.execute(
                select(TableRecord, TableSemantic)
                .outerjoin(TableSemantic, (TableSemantic.table_id == TableRecord.id)
                           & TableSemantic.is_current.is_(True))
                .where(TableRecord.data_source_id == ds.id,
                       TableRecord.is_tombstoned.is_(False)))).all()
            current = {oc.table_id: oc for oc in (await s.scalars(select(ObjectCategory).where(
                ObjectCategory.data_source_id == ds.id,
                ObjectCategory.is_current.is_(True)))).all()}
            keys = {f"{t.schema_name}.{t.object_name}".lower(): t for t, _ in rows}
            by_name: dict[str, list[str]] = {}
            for k in keys:
                by_name.setdefault(k.rsplit(".", 1)[1], []).append(k)
            definitions = {f"{d.schema_name}.{d.object_name}".lower(): d.definition
                           for d in (await s.scalars(select(ModuleDefinition).where(
                               ModuleDefinition.data_source_id == ds.id,
                               ModuleDefinition.is_current.is_(True)))).all()}
            result = {"proposed": 0, "unchanged": 0, "confirmed_kept": 0, "none": 0,
                      "why_none": {}}
            decided: dict[str, uuid.UUID] = {}
            ordered = sorted(rows, key=lambda r: (r[0].kind == "view", r[0].schema_name,
                                                  r[0].object_name))
            for t, sem in ordered:
                key = f"{t.schema_name}.{t.object_name}".lower()
                cur = current.get(t.id)
                if cur is not None and cur.status == "confirmed":
                    result["confirmed_kept"] += 1
                    decided[key] = cur.category_id
                    continue
                best, why = None, ""
                if t.kind == "view" and definitions.get(key):
                    read = _tables_read(definitions[key], sqlglot_dialect(ds.dialect), by_name,
                                        t.schema_name) or set()
                    found = {decided.get(r) for r in read if r != key}
                    if len(found) == 1 and None not in found:
                        cat_id = found.pop()
                        best = next((c for c in cats if c.id == cat_id), None)
                        why = "reads only objects of this category"
                if best is None:
                    best, why = propose_for([
                        (t.schema_name, 3.0), (t.object_name, 2.0),
                        (sem.business_name if sem else None, 2.0),
                        (sem.entity if sem else None, 1.0),
                        (sem.purpose if sem else None, 0.5)], cats)
                if best is None:
                    result["none"] += 1
                    result["why_none"][f"{t.schema_name}.{t.object_name}"] = why
                    if cur is not None:
                        decided[key] = cur.category_id
                    continue
                decided[key] = best.id
                if cur is not None and cur.category_id == best.id:
                    result["unchanged"] += 1
                    continue
                if cur is not None and not replace_proposals:
                    result["unchanged"] += 1
                    continue
                await _replace(s, ds.id, t.id, best.id, status="proposed", source="gaia",
                               actor=None, reason=why, at=self.now())
                result["proposed"] += 1
            await audit.record(s, "categories_proposed", actor=actor, subject=ds.name,
                               detail={k: v for k, v in result.items() if k != "why_none"})
            await s.commit()
            return result

    # -- suggestions and reports ------------------------------------------------------------

    async def role_suggestions(self) -> list[dict[str, Any]]:
        """For every directory role: the categories its name and description suggest."""
        async with self.factory() as s:
            cats = list((await s.scalars(select(Category).where(
                Category.retired_at.is_(None)))).all())
            roles = (await s.execute(select(DirectoryRole, Directory.name).join(
                Directory, Directory.id == DirectoryRole.directory_id).where(
                DirectoryRole.gone_at.is_(None)))).all()
        out = []
        for r, directory in roles:
            words = set(tokens(r.name)) | set(tokens(r.description))
            hits = [c.name for c in cats if words & _vocabulary(c)]
            if hits:
                out.append({"directory_role_id": str(r.id), "directory": directory,
                            "role": r.name, "categories": sorted(hits)})
        return out

    async def report(self, accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The auditor's view: each category, its objects per data source, who may read it
        (and through what)."""
        async with self.factory() as s:
            cats = (await s.scalars(select(Category).order_by(func.lower(Category.name)))).all()
            placed = (await s.execute(
                select(ObjectCategory.category_id, DataSource.name, TableRecord.schema_name,
                       TableRecord.object_name, ObjectCategory.status)
                .join(TableRecord, TableRecord.id == ObjectCategory.table_id)
                .join(DataSource, DataSource.id == ObjectCategory.data_source_id)
                .where(ObjectCategory.is_current.is_(True)))).all()
        out = []
        for c in cats:
            objs = [f"{ds}:{sch}.{obj}{'' if st == 'confirmed' else ' (proposed)'}"
                    for cid, ds, sch, obj, st in placed if cid == c.id]
            holders = [f"{a['login']} ({', '.join(a['via'].get(f'category:{c.id}', []))})"
                       for a in accounts if str(c.id) in (a.get("category_ids") or [])]
            out.append({"category": c.name, "retired": c.retired_at is not None,
                        "objects": sorted(objs), "holders": sorted(holders)})
        return out


# -- helpers -----------------------------------------------------------------------------------


def _clean(words: list[str] | None) -> list[str]:
    return sorted({w.strip() for w in words or [] if w and w.strip()})


async def _source(s: AsyncSession, name: str) -> DataSource:
    ds = await s.scalar(select(DataSource).where(DataSource.name == name))
    if ds is None or ds.status == "tombstoned":
        raise CategoryError(f"no data source {name!r}")
    return ds


async def _current(s: AsyncSession, table_id: uuid.UUID) -> ObjectCategory | None:
    return await s.scalar(select(ObjectCategory).where(
        ObjectCategory.table_id == table_id, ObjectCategory.is_current.is_(True)))


async def _replace(s: AsyncSession, ds_id: uuid.UUID, table_id: uuid.UUID,
                   category_id: uuid.UUID, *, status: str, source: str, actor: str | None,
                   reason: str | None, at: datetime) -> None:
    await s.execute(update(ObjectCategory).where(
        ObjectCategory.table_id == table_id, ObjectCategory.is_current.is_(True))
        .values(is_current=False).execution_options(synchronize_session=False))
    s.add(ObjectCategory(data_source_id=ds_id, table_id=table_id, category_id=category_id,
                         status=status, source=source, decided_by=actor, decided_at=at,
                         reason=reason, is_current=True))
    await s.flush()


def _category_view(c: Category, objects: int, grants: int) -> dict[str, Any]:
    return {"id": str(c.id), "name": c.name, "description": c.description,
            "keywords": list(c.keywords or []), "created_by": c.created_by,
            "created_at": c.created_at, "retired_at": c.retired_at, "objects": objects,
            "grants": grants}


def _object_view(t: TableRecord, sem: TableSemantic | None, oc: ObjectCategory | None,
                 c: Category | None) -> dict[str, Any]:
    return {"id": str(t.id), "name": f"{t.schema_name}.{t.object_name}", "kind": t.kind,
            "business_name": sem.business_name if sem else None,
            "category": None if oc is None or c is None else {
                "id": str(c.id), "name": c.name, "status": oc.status, "source": oc.source,
                "decided_by": oc.decided_by, "decided_at": oc.decided_at,
                "reason": oc.reason}}
