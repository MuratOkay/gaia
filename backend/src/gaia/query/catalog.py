"""What the SQL guard knows about a target (Phase 4A).

Two things, both from Gaia's metadata store, never from the target at query
time:

* **Objects and columns**: the latest catalog snapshot. A statement may only
  read objects in it — which also keeps it out of the system catalogs
  (``pg_catalog``, ``information_schema``, ``sys``), because those are never
  in the snapshot.
* **Join paths** (PHASES, Phase 4 validation rules; D48): declared foreign keys;
  model join hints that are ``inferred`` *and* whose Stage 3 test of the claim
  passed and was specific; SME-confirmed hints; and view pass-through lineage
  (a view column *is* its base column). Stage 1 join candidates are evidence,
  not join paths (D37), and ``stale`` / ``refuted`` hints never count.

Join paths are kept as **key classes**: every FK, hint or SME edge joins two
columns, and the columns an edge chain connects form one class (union-find). View
lineage is not an edge but an **alias**: a pass-through view column *is* its base
column, so it belongs to the base column's class. A join condition ``a = b`` is
on a join path when ``a`` and ``b`` (through their aliases) are in one class — the
parent and child of an FK, or two children of one key.

**Uniqueness** is the second half of the rule (the validator applies it): one
side of every join must be unique, or the join multiplies rows — the *fan trap*:
``order_lines JOIN payments ON order_id`` pairs every payment with every line of
its order, and a sum over it is silently wrong. Unique here means a primary key,
a single-column unique constraint or an unfiltered single-column unique index
(``unique``); the validator adds what a query makes unique (a CTE grouped by that
column). Composite keys are reduced to their column pairs: looser than the key,
never stricter.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from gaia.introspect.snapshot import Snapshot

__all__ = ["GuardCatalog", "JoinEdge", "load_guard_catalog"]


@dataclass(frozen=True)
class JoinEdge:
    """``from_table.from_column`` holds values of ``to_table.to_column``."""

    from_table: str  # schema.table
    from_column: str
    to_table: str
    to_column: str
    kind: str  # fk | hint | sme | lineage (an alias: view column = base column) | via_view

    @property
    def left(self) -> str:
        return f"{self.from_table}.{self.from_column}".lower()

    @property
    def right(self) -> str:
        return f"{self.to_table}.{self.to_column}".lower()


@dataclass
class GuardCatalog:
    dialect: str
    database: str
    # schema -> object -> {column: data_type}, names as in the catalog
    objects: dict[str, dict[str, dict[str, str]]]
    edges: list[JoinEdge] = field(default_factory=list)
    snapshot_id: str | None = None
    # "schema.object.column" (lower case) that are unique on their own
    unique: set[str] = field(default_factory=set)
    # need-to-know (5C): personal-data columns this person may only show or count
    show_only: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._direct: dict[frozenset[str], str] = {}
        self.unique = {u.lower() for u in self.unique}
        self.aliases: dict[str, str] = {
            e.left: e.right for e in self.edges if e.kind == "lineage"}
        for e in self.edges:
            if e.kind == "lineage":
                continue
            a, b = self.base(e.left), self.base(e.right)
            self._union(a, b)
            self._direct.setdefault(frozenset((a, b)), e.kind)

    def base(self, column: str) -> str:
        """The base column a view column passes through (view-on-view followed)."""
        column = column.lower()
        for _ in range(10):
            nxt = self.aliases.get(column)
            if nxt is None:
                return column
            column = nxt
        return column

    def is_unique(self, column: str) -> bool:
        return column.lower() in self.unique

    # -- objects -----------------------------------------------------------------------

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot, edges: Iterable[JoinEdge] = ()) -> GuardCatalog:
        objects: dict[str, dict[str, dict[str, str]]] = {}
        fk_edges = []
        unique: set[str] = set()
        for t in snapshot.tables:
            objects.setdefault(t.schema, {})[t.name] = {c.name: c.data_type for c in t.columns}
            keys = [c.columns for c in t.constraints if c.kind in ("primary_key", "unique")]
            keys += [[c["name"] for c in i.columns if not c.get("included")]
                     for i in t.indexes
                     if i.is_unique and not i.filter_definition and not i.is_disabled]
            for cols in keys:
                if len(cols) == 1:
                    unique.add(f"{t.schema}.{t.name}.{cols[0]}".lower())
            for r in t.relations:
                for a, b in zip(r.from_columns, r.to_columns, strict=False):
                    fk_edges.append(JoinEdge(f"{r.from_schema}.{r.from_table}", a,
                                             f"{r.to_schema}.{r.to_table}", b, "fk"))
        return cls(snapshot.dialect, snapshot.database, objects, [*fk_edges, *edges],
                   snapshot_id=snapshot.snapshot_id, unique=unique)

    def find(self, schema: str | None, name: str) -> list[tuple[str, str]]:
        """(schema, object) matches for a reference; case-insensitive, exact case wins."""
        hits = []
        for s, objs in self.objects.items():
            if schema is not None and s.lower() != schema.lower():
                continue
            hits += [(s, o) for o in objs if o.lower() == name.lower()]
        exact = [h for h in hits if h[1] == name and (schema is None or h[0] == schema)]
        return exact if len(hits) > 1 and exact else hits

    def columns(self, schema: str, name: str) -> dict[str, str]:
        return self.objects[schema][name]

    # -- join paths --------------------------------------------------------------------

    def _root(self, node: str) -> str:
        parent = self._parent.setdefault(node, node)
        if parent != node:
            parent = self._parent[node] = self._root(parent)
        return parent

    def _union(self, a: str, b: str) -> None:
        ra, rb = self._root(a), self._root(b)
        if ra != rb:
            self._parent[ra] = rb

    def join_kind(self, a: str, b: str) -> str | None:
        """Which join path connects ``a`` and ``b`` (``schema.table.column``, through view
        lineage), or None. Says nothing about fan-out: the validator checks uniqueness."""
        via_view = self.base(a) != a.lower() or self.base(b) != b.lower()
        a, b = self.base(a), self.base(b)
        if a == b:
            return "same_column"
        direct = self._direct.get(frozenset((a, b)))
        if direct:
            return f"{direct}_via_view" if via_view else direct
        if a in self._parent and b in self._parent and self._root(a) == self._root(b):
            return "shared_key"
        return None

    def join_edges(self) -> list[JoinEdge]:
        """Join paths between objects, for retrieval: the FK, hint and SME edges, and for a
        view column whose base column has one, the same edge from the view (``via_view``)."""
        real = [e for e in self.edges if e.kind != "lineage"]
        out = list(real)
        by_column: dict[str, list[JoinEdge]] = {}
        for e in real:
            by_column.setdefault(e.left, []).append(e)
            by_column.setdefault(e.right, []).append(e)
        for view_col, base_col in self.aliases.items():
            target = self.base(base_col)
            view_table, _, column = view_col.rpartition(".")
            name = self._proper(view_table)
            col = self._proper_column(name, column)
            base_table, _, base_column = target.rpartition(".")
            if self.is_unique(target):  # the view column *is* that key: join back 1:1
                proper = self._proper(base_table)
                out.append(JoinEdge(name, col, proper,
                                    self._proper_column(proper, base_column), "via_view"))
            for e in by_column.get(target, []):
                if e.from_table.lower() == e.to_table.lower():
                    continue  # a self-reference of the base table says nothing about the view
                if e.left == target:
                    other_table, other_col = e.to_table, e.to_column
                else:
                    other_table, other_col = e.from_table, e.from_column
                out.append(JoinEdge(name, col, other_table, other_col, "via_view"))
        return out

    def _proper(self, key: str) -> str:
        for s, objs in self.objects.items():
            for o in objs:
                if f"{s}.{o}".lower() == key.lower():
                    return f"{s}.{o}"
        return key

    def _proper_column(self, table: str, column: str) -> str:
        schema, _, name = table.partition(".")
        for c in self.objects.get(schema, {}).get(name, {}):
            if c.lower() == column.lower():
                return c
        return column


async def load_guard_catalog(session, data_source: str) -> tuple[GuardCatalog, uuid.UUID]:
    """The guard catalog of a data source and its data source id (read only)."""
    from sqlalchemy import select

    from gaia.db.models.profiling import VerificationTest
    from gaia.db.models.semantics import JoinHint
    from gaia.db.repositories.profile_store import ProfileStore
    from gaia.db.repositories.semantics_store import SemanticsStore
    from gaia.introspect.service import snapshot_from_document

    store = SemanticsStore(session)
    ds = await store.data_source(data_source)
    if ds is None:
        raise LookupError(f"no data source {data_source!r}; run `make introspect`")
    record = await store.latest_snapshot(ds.id)
    if record is None:
        raise LookupError(f"no snapshot for {data_source!r}; run `make introspect`")
    snapshot = snapshot_from_document(record.document)
    tables = await store.live_tables(ds.id)
    ids = {rec.id: f"{schema}.{name}" for (schema, name), rec in tables.items()}

    passed: set[tuple[str, str, str]] = set()  # (from table, column, test_id)
    tests = await session.scalars(select(VerificationTest).where(
        VerificationTest.data_source_id == ds.id, VerificationTest.is_current.is_(True),
        VerificationTest.passed.is_(True), VerificationTest.specific.is_(True)))
    for t in tests:
        if t.table_id in ids:
            passed.add((ids[t.table_id].lower(), t.column_name.lower(), t.test_id.lower()))

    edges: list[JoinEdge] = []
    hints = await session.scalars(select(JoinHint).where(JoinHint.data_source_id == ds.id))
    for h in hints:
        if len(h.from_columns) != 1 or len(h.to_columns) != 1:
            continue  # claim tests are single-column (D35)
        src, dst = f"{h.from_schema}.{h.from_table}", f"{h.to_schema}.{h.to_table}"
        col, to_col = h.from_columns[0], h.to_columns[0]
        if h.source == "sme" and h.status == "confirmed":
            edges.append(JoinEdge(src, col, dst, to_col, "sme"))
        elif h.source == "llm" and h.status == "inferred" and (
            src.lower(), col.lower(), f"inc:{dst}.{to_col}".lower()) in passed:
            edges.append(JoinEdge(src, col, dst, to_col, "hint"))  # D48
    for item in await ProfileStore(session).lineage(ds.id):
        if item.classification == "pass_through" and item.source:
            base_table, _, base_col = item.source.rpartition(".")
            edges.append(JoinEdge(f"{item.view_schema}.{item.view_name}", item.column_name,
                                  base_table, base_col, "lineage"))
    catalog = GuardCatalog.from_snapshot(snapshot, edges)
    catalog.snapshot_id = str(record.id)
    return catalog, ds.id
