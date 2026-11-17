"""Need-to-know, enforced (Phase 5C, D74).

**What a person may read.** Every object (table or view) of a data source carries one data
category (``auth.object_category``: proposed by Gaia, confirmed by an SME or set by an
administrator). A person may read an object's columns only if one of their grants holds
that category — nothing is readable until granted, and an object without a category is
readable by nobody (deny by default). A **view** needs its own category *and* whatever its
columns come from: a pass-through column needs its base column, a computed column all of
its inputs, and a column whose lineage is unknown every table the view's definition reads
(a view cannot be a way around a category).

**Personal data** (D69, settled here): a column that holds personal data — a detector
found it, the model's semantic type says so, or an SME stated it (and an SME's "no" wins)
— is shown unmasked only to a PII processor. For anyone else it may be *shown* — masked in
the result — or *counted*, nothing more: filtering, joining, grouping, sorting or computing
with it would reveal what the mask hides ("is there a customer with this e-mail?"), so the
guard refuses it (``personal_data``). A view column carries personal data if what it comes
from does.

**Where it is enforced — twice, both deterministic, never by a prompt:**

1. *Retrieval* gets an index restricted to what the person may read
   (``restrict_index``): the model never sees, and so cannot name, an object, column,
   value, join or glossary definition outside it.
2. *The SQL guard* works on the same restricted catalog (``AccessGuard``): a statement
   that names anything else fails to resolve, wherever it is (select list, filter, join,
   subquery, CTE, set operation); ``*`` is refused (it would be expanded by the database,
   not by the guard); and the full catalog is consulted only to tell a forbidden reference
   (``forbidden`` — refused, audited, never repaired) from a mistake (repaired as before).

The operator's command line (``make ask``, ``make golden``) runs with ``OPERATOR``, the full
policy: whoever runs it on the server already has every right there is. ``--as LOGIN``
runs them with a person's policy instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

import sqlglot
from sqlglot import exp

from gaia.query.catalog import GuardCatalog, JoinEdge
from gaia.query.retrieval import IndexedColumn, IndexedObject, RetrievalIndex
from gaia.query.validator import GuardResult, SqlGuardBase, Violation

__all__ = ["OPERATOR", "REFUSAL_CODES", "AccessFacts", "AccessGuard", "AccessPolicy",
           "compute_policy", "load_access_facts", "refusal_message", "restrict_index"]

REFUSAL_CODES = frozenset({"forbidden", "personal_data"})
_RESOLUTION = frozenset({"unknown_object", "unknown_column", "ambiguous_object", "unresolved"})


def refusal_message(code: str) -> str:
    if code == "personal_data":
        return ("This would filter, sort, group or calculate with personal data, which needs "
                "the PII-processor permission. Personal data can be shown (masked) or counted.")
    return ("This needs data your role does not cover. If you need it, ask your "
            "administrator.")


# -- facts about a data source (the same for everyone) --------------------------------------


@dataclass
class AccessFacts:
    """What the policy is computed from, loaded once per data source state."""

    objects: dict[str, str]  # key (lower) -> "schema.name" as in the catalog
    kinds: dict[str, str]  # key (lower) -> table | view
    columns: dict[str, list[str]]  # key (lower) -> column names (lower)
    category: dict[str, str | None]  # key (lower) -> category id in force, or None
    category_names: dict[str, str]  # id -> name
    # view column "schema.view.column" (lower) -> (classification, [input column keys])
    lineage: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    # view key (lower) -> object keys its definition reads (None: no definition known)
    view_tables: dict[str, set[str] | None] = field(default_factory=dict)
    view_text: dict[str, str] = field(default_factory=dict)  # view key -> definition (lower)
    personal: set[str] = field(default_factory=set)  # column keys (lower), own flag


async def load_access_facts(session, index: RetrievalIndex) -> AccessFacts:
    """From the (full) retrieval index and the metadata store: categories in force, view
    lineage and what each view's definition reads."""
    from sqlalchemy import select

    from gaia.db.models.auth import Category, ObjectCategory
    from gaia.db.models.catalog import TableRecord
    from gaia.db.models.profiling import ModuleDefinition
    from gaia.db.repositories.profile_store import ProfileStore
    from gaia.profiling.lineage import sqlglot_dialect

    ds_id = index.data_source_id
    objects = {k.lower(): k for k in index.objects}
    kinds = {k.lower(): o.kind for k, o in index.objects.items()}
    columns = {k.lower(): [c.name.lower() for c in o.columns] for k, o in index.objects.items()}
    personal = {f"{k}.{c.name}".lower() for k, o in index.objects.items()
                for c in o.columns if c.personal}
    rows = (await session.execute(
        select(TableRecord.schema_name, TableRecord.object_name, ObjectCategory.category_id,
               Category.retired_at)
        .join(ObjectCategory, ObjectCategory.table_id == TableRecord.id)
        .join(Category, Category.id == ObjectCategory.category_id)
        .where(ObjectCategory.data_source_id == ds_id, ObjectCategory.is_current.is_(True),
               TableRecord.is_tombstoned.is_(False)))).all()
    category: dict[str, str | None] = dict.fromkeys(objects)
    for schema, name, cat, retired in rows:
        key = f"{schema}.{name}".lower()
        if key in category and retired is None:
            category[key] = str(cat)
    names = {str(c.id): c.name for c in (await session.scalars(select(Category))).all()}

    lineage: dict[str, tuple[str, list[str]]] = {}
    for item in await ProfileStore(session).lineage(ds_id):
        key = f"{item.view_schema}.{item.view_name}.{item.column_name}".lower()
        inputs = [item.source] if item.classification == "pass_through" and item.source \
            else list(item.inputs or [])
        lineage[key] = (item.classification, [i.lower() for i in inputs])
    view_tables: dict[str, set[str] | None] = {k: None for k, kind in kinds.items()
                                               if kind == "view"}
    view_text: dict[str, str] = {}
    definitions = (await session.scalars(select(ModuleDefinition).where(
        ModuleDefinition.data_source_id == ds_id, ModuleDefinition.is_current.is_(True)))).all()
    by_name: dict[str, list[str]] = {}
    for key in objects:
        by_name.setdefault(key.rsplit(".", 1)[1], []).append(key)
    for d in definitions:
        key = f"{d.schema_name}.{d.object_name}".lower()
        if key not in view_tables or not d.definition:
            continue
        view_text[key] = d.definition.lower()
        view_tables[key] = _tables_read(d.definition, sqlglot_dialect(index.dialect), by_name,
                                        d.schema_name)
    return AccessFacts(objects, kinds, columns, category, names, lineage, view_tables,
                       view_text, personal)


def _tables_read(definition: str, dialect: str, by_name: dict[str, list[str]],
                 own_schema: str) -> set[str] | None:
    """The catalog objects a view's definition reads; None if it cannot be parsed (then
    the view's columns of unknown lineage are readable by nobody)."""
    try:
        tree = sqlglot.parse_one(definition, read=dialect)
    except Exception:  # noqa: BLE001 - any parser failure means "unknown"
        return None
    if tree is None:
        return None
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out: set[str] = set()
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            continue
        name = t.name.lower()
        if not t.db and name in ctes:
            continue
        if t.db:
            out.add(f"{t.db}.{name}".lower())
        else:  # unqualified: its own schema first, else every object of that name
            hits = by_name.get(name, [])
            own = [h for h in hits if h.startswith(f"{own_schema.lower()}.")]
            out.update(own or hits or [name])
    return out


# -- the policy of one person on one data source ----------------------------------------------


@dataclass(frozen=True)
class AccessPolicy:
    label: str  # the login, or "operator"
    full: bool = False
    pii_processor: bool = True
    categories: frozenset[str] = frozenset()
    readable: dict[str, frozenset[str]] = field(default_factory=dict)  # key -> columns
    personal: frozenset[str] = frozenset()  # column keys (lower), views included
    objects: dict[str, str] = field(default_factory=dict)  # key (lower) -> proper key

    def can_read(self, obj: str, column: str | None = None) -> bool:
        if self.full:
            return True
        cols = self.readable.get(obj.lower())
        if not cols:
            return False
        return column is None or column.lower() in cols

    @property
    def forbidden_objects(self) -> list[str]:
        return sorted(v for k, v in self.objects.items() if not self.readable.get(k))

    def mentions_forbidden(self, text: str | None) -> bool:
        """Whether a text names an object the person may not read (``schema.object``)."""
        if self.full or not text:
            return False
        low = text.lower()
        return any(re.search(rf"(?<![\w.]){re.escape(k)}(?![\w])", low)
                   for k in self.objects if not self.readable.get(k))

    @property
    def cache_key(self) -> tuple[Any, ...]:
        return (self.full, self.pii_processor, tuple(sorted(self.categories)))


OPERATOR = AccessPolicy(label="operator", full=True)


def compute_policy(facts: AccessFacts, *, categories: set[str] | frozenset[str],
                   pii_processor: bool, label: str) -> AccessPolicy:
    held = frozenset(str(c) for c in categories)

    def object_ok(key: str) -> bool:
        return facts.category.get(key) in held and facts.category.get(key) is not None

    memo: dict[str, bool] = {}

    def column_ok(col: str, depth: int = 0) -> bool:
        if col in memo:
            return memo[col]
        obj = col.rsplit(".", 1)[0]
        if obj not in facts.objects or depth > 10 or not object_ok(obj):
            memo[col] = False
            return False
        if facts.kinds.get(obj) != "view":
            memo[col] = True
            return True
        memo[col] = False  # a cycle reads nothing
        classification, inputs = facts.lineage.get(col, ("unresolved", []))
        if classification in ("pass_through", "computed"):
            ok = all(column_ok(i, depth + 1) if i.rsplit(".", 1)[0] in facts.objects
                     else False for i in inputs)
        else:  # unknown lineage: everything the definition reads
            tables = facts.view_tables.get(obj)
            if tables is None:
                ok = True  # no definition known: the view's own category decides
            else:
                ok = all(t in facts.objects and object_ok(t) and (
                    facts.kinds.get(t) != "view"
                    or all(column_ok(f"{t}.{c}", depth + 1) for c in facts.columns[t]))
                    for t in tables if t != obj)
        memo[col] = ok
        return ok

    pmemo: dict[str, bool] = {}

    def personal(col: str, depth: int = 0) -> bool:
        if col in pmemo:
            return pmemo[col]
        pmemo[col] = col in facts.personal
        obj = col.rsplit(".", 1)[0]
        if facts.kinds.get(obj) == "view" and depth < 10 and not pmemo[col]:
            classification, inputs = facts.lineage.get(col, ("unresolved", []))
            if classification in ("pass_through", "computed"):
                pmemo[col] = any(personal(i, depth + 1) for i in inputs)
            else:
                text = facts.view_text.get(obj, "")
                reads = facts.view_tables.get(obj) or set()
                pmemo[col] = any(p.rsplit(".", 1)[0] in reads
                                 and re.search(rf"\b{re.escape(p.rsplit('.', 1)[1])}\b", text)
                                 for p in facts.personal)
        return pmemo[col]

    readable: dict[str, frozenset[str]] = {}
    for key, cols in facts.columns.items():
        ok = frozenset(c for c in cols if column_ok(f"{key}.{c}"))
        if ok:
            readable[key] = ok
    all_personal = frozenset(f"{k}.{c}" for k, cols in facts.columns.items() for c in cols
                             if personal(f"{k}.{c}"))
    return AccessPolicy(label=label, full=False, pii_processor=pii_processor,
                        categories=held, readable=readable, personal=all_personal,
                        objects=dict(facts.objects))


# -- enforcement in retrieval -------------------------------------------------------------


def restrict_catalog(catalog: GuardCatalog, policy: AccessPolicy) -> GuardCatalog:
    objects: dict[str, dict[str, dict[str, str]]] = {}
    for schema, objs in catalog.objects.items():
        for name, cols in objs.items():
            allowed = {c: t for c, t in cols.items() if policy.can_read(f"{schema}.{name}", c)}
            if allowed:
                objects.setdefault(schema, {})[name] = allowed

    def readable(column_key: str) -> bool:
        obj, _, col = column_key.rpartition(".")
        return policy.can_read(obj, col)

    edges = [e for e in catalog.edges if readable(e.left) and readable(e.right)]
    restricted = GuardCatalog(catalog.dialect, catalog.database, objects,
                              [JoinEdge(e.from_table, e.from_column, e.to_table, e.to_column,
                                        e.kind) for e in edges],
                              snapshot_id=catalog.snapshot_id,
                              unique={u for u in catalog.unique if readable(u)})
    if not policy.pii_processor:
        restricted.show_only = {p for p in policy.personal if readable(p)}
    return restricted


def restrict_index(index: RetrievalIndex, policy: AccessPolicy) -> RetrievalIndex:
    """The index a person's questions are answered from. The full policy returns the index
    itself, so the operator's runs see exactly what they saw before (D72)."""
    if policy.full:
        return index
    objects: dict[str, IndexedObject] = {}
    for key, o in index.objects.items():
        cols = policy.readable.get(key.lower())
        if not cols:
            continue
        kept: list[IndexedColumn] = []
        for c in o.columns:
            if c.name.lower() not in cols:
                continue
            is_personal = f"{key}.{c.name}".lower() in policy.personal
            if is_personal and not policy.pii_processor:
                kept.append(replace(c, personal=True, show_only=True, values=[],
                                    meanings={}, range=None,
                                    value_note="personal data: values not shown"))
            else:
                kept.append(replace(c, personal=c.personal or is_personal))
        objects[key] = replace(o, columns=kept,
                               primary_key=[k for k in o.primary_key if k.lower() in cols])
    forbidden = [k for k in index.objects if not policy.readable.get(k.lower())]
    glossary = [(t, d, syn) for t, d, syn in index.glossary
                if not any(re.search(rf"(?<![\w.]){re.escape(f.lower())}(?![\w])", d.lower())
                           for f in forbidden)]
    return RetrievalIndex(index.data_source, index.dialect, index.database, objects,
                          restrict_catalog(index.catalog, policy),
                          data_source_id=index.data_source_id, snapshot_id=index.snapshot_id,
                          glossary=glossary, mask_personal=not policy.pii_processor)


# -- enforcement in the guard ---------------------------------------------------------------


class AccessGuard:
    """The SQL guard on the restricted catalog; the full catalog only tells a forbidden
    reference from a mistake. Quacks like ``SqlGuardBase`` (``check``, ``validate``, and
    the restricted guard's attributes)."""

    def __init__(self, restricted: SqlGuardBase, full: SqlGuardBase, policy: AccessPolicy
                 ) -> None:
        restricted.forbid_star = True
        self.restricted, self.full, self.policy = restricted, full, policy

    def __getattr__(self, name: str) -> Any:
        return getattr(self.restricted, name)

    def validate(self, sql: str) -> list[str]:
        return [str(v) for v in self.check(sql).errors]

    def check(self, sql: str) -> GuardResult:
        result = self.restricted.check(sql)
        if result.ok or not any(v.code in _RESOLUTION for v in result.errors):
            return result
        full = self.full.check(sql)
        # Reaching for data outside the role decides the turn, whatever else is wrong with
        # the statement (D76): the model never sees the forbidden tables' columns, so a
        # statement that reaches for them usually guesses a column name too — and a
        # "mistake" would be sent back for repair, telling the model the table is "not in
        # the catalog" and inviting another try, and end as a failure nobody audits.
        reached = [t for t in full.tables if not self.policy.can_read(t)]
        if not reached:
            reached = self._forbidden_columns(sql)
        if not reached and any(v.code in _RESOLUTION for v in full.errors):
            return result  # a genuine mistake on data the person may read: repaired
        out = GuardResult(result.dialect, sql, tables=full.tables)
        out.errors.append(Violation("forbidden", refusal_message("forbidden")))
        out.forbidden = reached or ["a column outside the role"]
        return out

    def _forbidden_columns(self, sql: str) -> list[str]:
        """Columns the statement names through a table alias that exist in the full catalog
        but are not the person's — found without resolving the whole statement, which may
        not resolve (a guessed column beside them)."""
        import sqlglot
        from sqlglot import exp

        catalog = self.full.catalog
        try:
            trees = [t for t in sqlglot.parse(sql, read=self.full.read) if t is not None]
        except Exception:  # unparseable: the restricted guard has said so already
            return []
        out: set[str] = set()
        for tree in trees:
            aliases: dict[str, tuple[str, str]] = {}
            for table in tree.find_all(exp.Table):
                if not isinstance(table.this, exp.Identifier):
                    continue
                hits = catalog.find(table.db or None, table.name)
                if len(hits) == 1:
                    aliases[table.alias_or_name.lower()] = hits[0]
                    aliases.setdefault(table.name.lower(), hits[0])
            for column in tree.find_all(exp.Column):
                hit = aliases.get((column.table or "").lower())
                if hit is None:
                    continue
                cols = {c.lower() for c in catalog.columns(*hit)}
                key = f"{hit[0]}.{hit[1]}"
                if column.name.lower() in cols and not self.policy.can_read(key, column.name):
                    out.add(f"{key}.{column.name.lower()}")
        return sorted(out)


def build_access_guard(settings, label: str, index: RetrievalIndex, full_catalog: GuardCatalog,
                       policy: AccessPolicy):
    """The guard for a person's question: ``AccessGuard``, or the plain guard for the
    operator."""
    from gaia.query.guard import build_guard

    if policy.full:
        return build_guard(settings, label, full_catalog)
    return AccessGuard(build_guard(settings, label, index.catalog),
                       build_guard(settings, label, full_catalog), policy)


def policy_for(facts: AccessFacts, effective: Any, label: str) -> AccessPolicy:
    """The policy of a signed-in person (``auth.privileges.Effective``)."""
    return compute_policy(facts, categories=set(effective.categories),
                          pii_processor=bool(effective.pii_processor), label=label)



async def policy_for_login(session, index: RetrievalIndex, login: str,
                           directory: str | None = None) -> AccessPolicy:
    """A person's policy from the metadata store (their grants and the directory roles last
    read for them) — for ``make ask --as LOGIN`` and ``make golden --as LOGIN``, which answer
    exactly as the chat would answer that person."""
    from sqlalchemy import select

    from gaia.auth.service import effective_of
    from gaia.db.models.auth import Account, Directory

    q = select(Account).where(Account.login == login.strip().lower(),
                              Account.is_local.is_(False))
    if directory:
        q = q.join(Directory, Directory.id == Account.directory_id).where(
            Directory.name == directory)
    found = (await session.scalars(q)).all()
    if len(found) != 1:
        raise LookupError(f"{len(found)} account(s) with login {login!r}"
                          + ("" if directory else " (name the directory with --directory)"))
    eff = await effective_of(session, found[0])
    facts = await load_access_facts(session, index)
    return policy_for(facts, eff, found[0].login)
