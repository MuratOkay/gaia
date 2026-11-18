"""Data categories (Phase 5C, D70, D74): defined by the administrator, proposed for each
object by Gaia, confirmed by an SME (what the data *is*), granted by the administrator
(who may *see* it).

**Gaia's proposal is deterministic** — no model decides anything about access. Since D77
(``gaia.access.proposal``) an object is matched against each category's name and keywords
in its schema, name and business name (*direct*) or only in what Gaia wrote about it
(*weak*), and it follows the table it is named after and the tables it references
(*related*); a view whose definition reads objects of one category gets that category. A
tie, or no match, proposes nothing: the object stays readable by nobody until someone
decides. Each proposal keeps its basis and reasons; proposals never replace a confirmed
category.

**Suggestions for directory roles** work the same way over a role's name and description
("GAIA-Finance" → Finance). A suggestion grants nothing.

Every change is in the hash-chained audit (D70).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.access.proposal import word_stems
from gaia.auth import audit
from gaia.db.models.auth import Category, Directory, DirectoryRole, Grant, ObjectCategory
from gaia.db.models.catalog import DataSource, TableRecord
from gaia.db.models.semantics import TableSemantic

__all__ = ["GENERIC_SCHEMAS", "CategoryError", "CategoryService"]


class CategoryError(RuntimeError):
    """A request that cannot be done as asked (400)."""


# schemas that say nothing about the data they hold: no category is made from them
GENERIC_SCHEMAS = frozenset({"dbo", "public", "guest", "main", "default", "sys", "app"})


def _terms(c: Category):
    from gaia.access.proposal import CategoryTerms

    return CategoryTerms(c.id, c.name, frozenset(_vocabulary(c)))


def _vocabulary(c: Category) -> set[str]:
    """The literal stems of its name and keywords (D77: "HR" is "hr" — retrieval's
    tokenizer would read it as the abbreviation of "hour")."""
    from gaia.access.proposal import literal_stems

    words = literal_stems(c.name)
    for k in c.keywords or []:
        words |= literal_stems(k)
    return words


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
        """A starting point: one category per schema of the data source, named after it;
        its keywords are the schema's name and the words its tables are named with
        (``sales`` → order, payment, return …), so the category also recognises that kind of
        data in another data source (D77). Schemas that say nothing about the data —
        ``dbo``, ``public`` — get none: define those categories by hand. A category of that
        name made from this schema before gets the words it lacks."""
        from gaia.access.proposal import schema_keywords

        async with self.factory() as s:
            ds = await _source(s, data_source)
            rows = (await s.execute(select(TableRecord.schema_name, TableRecord.object_name,
                                           TableRecord.kind).where(
                TableRecord.data_source_id == ds.id,
                TableRecord.is_tombstoned.is_(False)))).all()
        schemas = sorted({r[0] for r in rows})
        tables: dict[str, list[str]] = {}
        for schema, name, kind in rows:
            if kind != "view" and schema.lower() not in GENERIC_SCHEMAS:
                tables.setdefault(schema, []).append(name)
        words = schema_keywords(tables)
        out = []
        for schema in schemas:
            if schema.lower() in GENERIC_SCHEMAS:
                continue
            name = schema.replace("_", " ").strip().title() if schema.islower() else schema
            keywords = [schema, *words.get(schema, [])]
            description = f"the {schema} schema of {data_source}"
            try:
                out.append({**await self.add(name, actor=actor, keywords=keywords,
                                             description=description), "action": "added"})
            except CategoryError:
                async with self.factory() as s:
                    c = await s.scalar(select(Category).where(
                        func.lower(Category.name) == name.lower()))
                    missing = sorted(set(keywords) - set(c.keywords or [])) if c else []
                    if c is None or c.description != description or not missing:
                        continue
                before = list(c.keywords or [])
                out.append({**await self.change(str(c.id), actor=actor,
                                                keywords=[*before, *missing]),
                            "action": "completed", "added_keywords": missing})
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

    async def _facts(self, s: AsyncSession, ds: DataSource) -> dict[str, Any]:
        """Everything the proposal rule reads about a data source: its objects (with their
        words, references, what views read, and any confirmed category), the categories'
        vocabularies, and what people confirmed elsewhere."""
        from gaia.access.policy import _tables_read
        from gaia.access.proposal import ObjectFacts
        from gaia.db.models.catalog import ConstraintRecord, RelationRecord
        from gaia.db.models.profiling import ModuleDefinition
        from gaia.db.models.semantics import JoinHint
        from gaia.profiling.lineage import sqlglot_dialect

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
        # what each table points at: declared foreign keys, and joins an SME confirmed;
        # a foreign key inside the primary key makes the table a part of the other (D79)
        pks = {t_id: {c.lower() for c in cols} for t_id, cols in (await s.execute(
            select(ConstraintRecord.table_id, ConstraintRecord.column_names).join(
                TableRecord, TableRecord.id == ConstraintRecord.table_id).where(
                TableRecord.data_source_id == ds.id,
                ConstraintRecord.kind == "primary_key"))).all()}
        refs: dict[str, set[str]] = {}
        owners: dict[str, set[str]] = {}
        for rel, t in (await s.execute(
                select(RelationRecord, TableRecord).join(
                    TableRecord, TableRecord.id == RelationRecord.table_id).where(
                    TableRecord.data_source_id == ds.id,
                    TableRecord.is_tombstoned.is_(False)))).all():
            key = f"{t.schema_name}.{t.object_name}".lower()
            target = f"{rel.to_schema}.{rel.to_table}".lower()
            refs.setdefault(key, set()).add(target)
            fk = {c.lower() for c in rel.from_columns or []}
            if fk and fk <= pks.get(t.id, set()):
                owners.setdefault(key, set()).add(target)
        for h in (await s.scalars(select(JoinHint).where(
                JoinHint.data_source_id == ds.id, JoinHint.status == "confirmed",
                JoinHint.source.in_(("fk", "sme"))))).all():
            refs.setdefault(f"{h.from_schema}.{h.from_table}".lower(), set()).add(
                f"{h.to_schema}.{h.to_table}".lower())
        facts = []
        for t, sem in rows:
            key = f"{t.schema_name}.{t.object_name}".lower()
            cur = current.get(t.id)
            reads = None
            if t.kind == "view" and definitions.get(key):
                reads = _tables_read(definitions[key], sqlglot_dialect(ds.dialect),
                                     by_name, t.schema_name) or set()
            facts.append(ObjectFacts(
                key=key, schema=t.schema_name, name=t.object_name,
                kind="view" if t.kind == "view" else "table",
                business_name=sem.business_name if sem else None,
                entity=sem.entity if sem else None, purpose=sem.purpose if sem else None,
                references=refs.get(key, set()), identifying=owners.get(key, set()),
                reads=reads,
                confirmed=cur.category_id if cur is not None and cur.status == "confirmed"
                else None))
        # what people confirmed in the other data sources: evidence for objects named
        # like them (D79)
        elsewhere = [ObjectFacts(key=f"{dsn}:{sch}.{obj}".lower(), schema=sch, name=obj,
                                 confirmed=cat_id)
                     for dsn, sch, obj, cat_id in (await s.execute(
                         select(DataSource.name, TableRecord.schema_name,
                                TableRecord.object_name, ObjectCategory.category_id)
                         .join(TableRecord, TableRecord.id == ObjectCategory.table_id)
                         .join(DataSource, DataSource.id == ObjectCategory.data_source_id)
                         .where(ObjectCategory.is_current.is_(True),
                                ObjectCategory.status == "confirmed",
                                ObjectCategory.data_source_id != ds.id))).all()]
        return {"cats": cats, "rows": rows, "current": current, "facts": facts,
                "terms": [_terms(c) for c in cats], "elsewhere": elsewhere}

    async def propose(self, data_source: str, *, actor: str = "gaia",
                      replace_proposals: bool = True) -> dict[str, Any]:
        """Gaia's proposal for every object without a confirmed category — from its own
        words, the tables it is named after or references, and for a view what it reads
        (``gaia.access.proposal``, D77). Confirmed categories are kept, and steer their
        neighbours."""
        from gaia.access.proposal import propose_all

        async with self.factory() as s:
            ds = await _source(s, data_source)
            f = await self._facts(s, ds)
            rows, current, facts = f["rows"], f["current"], f["facts"]
            terms, elsewhere = f["terms"], f["elsewhere"]
            proposals = propose_all(facts, terms, elsewhere)
            result: dict[str, Any] = {"proposed": 0, "unchanged": 0, "confirmed_kept": 0,
                                      "none": 0, "by_basis": {}, "why_none": {}}
            for t, _sem in sorted(rows, key=lambda r: (r[0].schema_name, r[0].object_name)):
                key = f"{t.schema_name}.{t.object_name}".lower()
                cur, p = current.get(t.id), proposals[key]
                if cur is not None and cur.status == "confirmed":
                    result["confirmed_kept"] += 1
                    continue
                if p.category is None:
                    result["none"] += 1
                    result["why_none"][f"{t.schema_name}.{t.object_name}"] = p.why
                    if cur is not None and cur.source == "gaia":
                        # an earlier proposal stays (taking it away would take away access
                        # without anyone deciding), but says what it is now (D79)
                        kept = f"an earlier proposal, kept; now {p.why}"
                        if (cur.reason, cur.basis) != (kept, "weak"):
                            cur.reason, cur.basis = kept, "weak"
                    continue
                result["by_basis"][p.basis] = result["by_basis"].get(p.basis, 0) + 1
                if cur is not None and cur.category_id == p.category.id:
                    if (cur.reason, cur.basis) != (p.why, p.basis):  # same category, newer why
                        cur.reason, cur.basis = p.why, p.basis
                    result["unchanged"] += 1
                    continue
                if cur is not None and not replace_proposals:
                    result["unchanged"] += 1
                    continue
                await _replace(s, ds.id, t.id, p.category.id, status="proposed", source="gaia",
                               actor=None, reason=p.why, at=self.now(), basis=p.basis)
                result["proposed"] += 1
            await audit.record(s, "categories_proposed", actor=actor, subject=ds.name,
                               detail={k: v for k, v in result.items() if k != "why_none"})
            await s.commit()
            return result

    async def accuracy(self, data_source: str) -> dict[str, Any]:
        """6G (D88): how often Gaia's proposal is what people confirmed, per basis.

        * **today's rule, leave-one-out** — for every confirmed object, the proposal the rule
          makes with that one confirmation hidden (its neighbours' stay, as they would for a new
          object): agreement per basis, the objects it would get wrong, and those it would leave
          without a proposal;
        * **the proposals people saw** — the latest proposal Gaia had made before each
          confirmation (whatever rule made it then), and whether the person kept it.

        People confirmed with Gaia's proposal on screen, so both lean towards agreement (as
        6B's anchoring does); the blind share does not exist here."""
        from gaia.access.proposal import propose_all

        async with self.factory() as s:
            ds = await _source(s, data_source)
            f = await self._facts(s, ds)
            names = {c.id: c.name for c in f["cats"]}
            by_key = {t.id: f"{t.schema_name}.{t.object_name}" for t, _ in f["rows"]}
            history = (await s.scalars(select(ObjectCategory).where(
                ObjectCategory.data_source_id == ds.id).order_by(
                ObjectCategory.table_id, ObjectCategory.decided_at))).all()
        confirmed = [x for x in f["facts"] if x.confirmed is not None]
        rule: dict[str, Any] = {"n": len(confirmed), "agree": 0, "none": 0, "by_basis": {},
                                "misses": [], "unproposed": []}
        for obj in confirmed:
            truth, obj.confirmed = obj.confirmed, None
            try:
                p = propose_all(f["facts"], f["terms"], f["elsewhere"])[obj.key]
            finally:
                obj.confirmed = truth
            name = f"{obj.schema}.{obj.name}"
            if p.category is None:
                rule["none"] += 1
                rule["unproposed"].append({"object": name, "confirmed": names.get(truth),
                                           "why": p.why})
                continue
            hit = p.category.id == truth
            b = rule["by_basis"].setdefault(p.basis or "?", [0, 0])
            b[0] += hit
            b[1] += 1
            rule["agree"] += hit
            if not hit:
                rule["misses"].append({"object": name, "proposed": p.category.name,
                                       "confirmed": names.get(truth), "basis": p.basis,
                                       "why": p.why})
        seen: dict[str, Any] = {"n": 0, "kept": 0, "no_proposal": 0, "by_basis": {}}
        last_proposal: dict[Any, ObjectCategory] = {}
        decided: set[Any] = set()
        for oc in history:
            if oc.source == "gaia":
                last_proposal[oc.table_id] = oc
                continue
            if oc.status != "confirmed" or oc.table_id not in by_key or oc.table_id in decided:
                continue  # a person's second decision on an object is not about a proposal
            decided.add(oc.table_id)
            seen["n"] += 1
            before = last_proposal.pop(oc.table_id, None)
            if before is None:
                seen["no_proposal"] += 1
                continue
            kept = before.category_id == oc.category_id
            b = seen["by_basis"].setdefault(before.basis or "before D77", [0, 0])
            b[0] += kept
            b[1] += 1
            seen["kept"] += kept
        return {"data_source": data_source, "rule": rule, "seen": seen}

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
            words = {w for text in re.findall(r"[A-Za-z0-9]+", f"{r.name} {r.description or ''}")
                     for w in word_stems(text)}
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
                   reason: str | None, at: datetime, basis: str | None = None) -> None:
    await s.execute(update(ObjectCategory).where(
        ObjectCategory.table_id == table_id, ObjectCategory.is_current.is_(True))
        .values(is_current=False).execution_options(synchronize_session=False))
    s.add(ObjectCategory(data_source_id=ds_id, table_id=table_id, category_id=category_id,
                         status=status, source=source, decided_by=actor, decided_at=at,
                         reason=reason, is_current=True, basis=basis))
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
                "reason": oc.reason, "basis": oc.basis}}
