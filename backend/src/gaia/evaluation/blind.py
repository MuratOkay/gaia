"""Name-blind worlds for the Phase 3C evaluation (PHASES, "Evaluation design").

The worst case the product is built for has meaningless names. ``nge_small`` has
descriptive ones, so it can *simulate* the worst case: every schema, table,
column, constraint, index and module identifier is replaced by an opaque token
(``s02.t007.c04``), the definitions that mention them (checks, computed
columns, defaults, filters, view expressions, identities, test details) are
rewritten, and comments are dropped. **Data values are untouched** — profiles,
samples, detectors and tests see exactly what they saw before. The hidden
original names then serve as the reference meaning the judge compares with.

Everything here is a pure function of a snapshot and its evidence; the blinded
world is never stored. Tokens are assigned in a seeded random order, so they
carry no alphabetical hint of the original names.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field, replace

from gaia.introspect.snapshot import (
    ColumnSnapshot,
    ConstraintSnapshot,
    IndexSnapshot,
    RelationSnapshot,
    Snapshot,
    TableSnapshot,
)
from gaia.profiling.summary import (
    ColumnProfileSummary,
    IdentityRelation,
    LineageInfo,
    StaticEvidence,
    TableEvidenceInput,
    VerificationResult,
    ViewAlias,
)

__all__ = ["Blinder", "World", "real_world"]

# Words a rewritten definition may keep although they look like identifiers.
_QUOTED = re.compile(r"'(?:[^']|'')*'")
_WORD = re.compile(r'\[([^\]]+)\]|"([^"]+)"|\b([A-Za-z_][A-Za-z0-9_$#@]*)\b')
_SEQUENCE = re.compile(r"nextval\('[^']*'(::regclass)?\)", re.IGNORECASE)
_DOMAIN = re.compile(r"^(domain\s+)(\S+?)(:)", re.IGNORECASE)


@dataclass
class World:
    """What one evaluation condition shows the model: a snapshot and evidence,
    plus the way back from its names to the real ones."""

    snapshot: Snapshot
    evidence: StaticEvidence
    blind: bool = False
    # world "schema.table" -> real "schema.table"; world column -> real column
    tables: dict[str, str] = field(default_factory=dict)
    columns: dict[tuple[str, str], str] = field(default_factory=dict)

    def real_table(self, qualified: str) -> str:
        return self.tables.get(qualified, qualified)

    def real_column(self, qualified_table: str, column: str) -> str:
        return self.columns.get((qualified_table, column), column)

    def world_table(self, real: str) -> str:
        for world, actual in self.tables.items():
            if actual == real:
                return world
        return real


def real_world(snapshot: Snapshot, evidence: StaticEvidence) -> World:
    """Names visible: the world is the database itself."""
    return World(snapshot, evidence, blind=False)


class Blinder:
    """Opaque names for one snapshot. Build once, then blind the snapshot and
    any evidence about it consistently."""

    def __init__(self, snapshot: Snapshot, seed: int = 42) -> None:
        self.source = snapshot
        rng = random.Random(seed)  # noqa: S311 (token order, not security)
        schemas = sorted({t.schema for t in snapshot.tables})
        rng.shuffle(schemas)
        self.schema_map = {s: f"s{i:02d}" for i, s in enumerate(schemas, start=1)}
        tables = sorted(snapshot.tables, key=lambda t: t.qualified_name)
        rng.shuffle(tables)
        self.table_map: dict[str, tuple[str, str]] = {}  # real qualified -> (schema, name)
        for i, t in enumerate(tables, start=1):
            prefix = "v" if t.kind == "view" else "t"
            self.table_map[t.qualified_name] = (self.schema_map[t.schema], f"{prefix}{i:03d}")
        self.column_map: dict[str, dict[str, str]] = {}  # real qualified -> real col -> token
        for t in snapshot.tables:
            ordered = sorted(t.columns, key=lambda c: c.ordinal)
            self.column_map[t.qualified_name] = {
                c.name: f"c{i:02d}" for i, c in enumerate(ordered, start=1)
            }
        self._object_names: dict[str, str] = {}
        # every identifier, case-insensitive, for free text (details, expressions)
        self._words: dict[str, str] = {}
        for real, (schema, name) in self.table_map.items():
            real_schema, real_name = real.split(".", 1)
            self._words.setdefault(real_schema.lower(), schema)
            self._words.setdefault(real_name.lower(), name)

    # -- names ------------------------------------------------------------------------

    def table(self, real_qualified: str) -> str:
        schema, name = self.table_map[real_qualified]
        return f"{schema}.{name}"

    def column(self, real_qualified: str, column: str) -> str:
        return self.column_map.get(real_qualified, {}).get(column, column)

    def _object(self, kind: str, real: str) -> str:
        key = f"{kind}:{real}"
        if key not in self._object_names:
            self._object_names[key] = f"{kind}{len(self._object_names) + 1:03d}"
        return self._object_names[key]

    # -- text -------------------------------------------------------------------------

    def text(self, text: str | None, table: str | None = None) -> str | None:
        """Replace identifiers in free text and SQL fragments: qualified names
        first, then single words that are a column of ``table`` (when given),
        or any table or schema name. Quoted string literals (data values) are
        kept as they are."""
        if text is None:
            return None
        columns = {c.lower(): tok for c, tok in self.column_map.get(table or "", {}).items()}
        # 1. schema.table.column and schema.table, longest first
        out = text
        for real in sorted(self.table_map, key=len, reverse=True):
            world = self.table(real)
            for col, tok in sorted(self.column_map.get(real, {}).items(),
                                   key=lambda kv: -len(kv[0])):
                out = re.sub(rf"\b{re.escape(real)}\.{re.escape(col)}\b",
                             f"{world}.{tok}", out, flags=re.IGNORECASE)
            out = re.sub(rf"\b{re.escape(real)}\b", world, out, flags=re.IGNORECASE)
        out = _SEQUENCE.sub("nextval('seq')", out)

        # 2. single words outside string literals
        parts: list[str] = []
        last = 0
        for literal in _QUOTED.finditer(out):
            parts.append(self._words_in(out[last:literal.start()], columns))
            parts.append(literal.group(0))
            last = literal.end()
        parts.append(self._words_in(out[last:], columns))
        return "".join(parts)

    def _words_in(self, text: str, columns: dict[str, str]) -> str:
        def swap(match: re.Match) -> str:
            word = match.group(1) or match.group(2) or match.group(3)
            low = word.lower()
            if match.group(3) is not None and low in _SQL_WORDS:
                return match.group(0)  # a keyword or type, not an identifier
            token = columns.get(low) or self._words.get(low) or self._any_column(low)
            if token is None:
                return match.group(0)
            if match.group(1) is not None:
                return f"[{token}]"
            if match.group(2) is not None:
                return f'"{token}"'
            return token

        return _WORD.sub(swap, text)

    def _any_column(self, low: str) -> str | None:
        """A column name of another table (e.g. inside a view expression)."""
        for mapping in self.column_map.values():
            for real, tok in mapping.items():
                if real.lower() == low:
                    return tok
        return None

    # -- snapshot ---------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        tables = [self._table(t) for t in self.source.tables]
        return replace(self.source, database="database", tables=tables)

    def _table(self, t: TableSnapshot) -> TableSnapshot:
        real = t.qualified_name
        schema, name = self.table_map[real]
        cols = self.column_map[real]
        if cols is None:
            return t
        return replace(
            t,
            schema=schema,
            name=name,
            comment=None,
            columns=[self._column(real, c) for c in t.columns],
            constraints=[self._constraint(real, c) for c in t.constraints],
            relations=[self._relation(r) for r in t.relations],
            indexes=[self._index(real, i) for i in t.indexes],
        )

    def _column(self, table: str, c: ColumnSnapshot) -> ColumnSnapshot:
        type_definition = c.type_definition
        data_type = c.data_type
        if type_definition:
            if type_definition.lower().startswith("enum"):
                data_type = "enum"  # the enum's name is a name; its labels are data
            type_definition = _DOMAIN.sub(r"\1domain\3", type_definition)
            type_definition = self.text(type_definition, table)
        return replace(
            c,
            name=self.column(table, c.name),
            data_type=data_type,
            comment=None,
            default=self.text(c.default, table),
            computed_definition=self.text(c.computed_definition, table),
            type_definition=type_definition,
            collation=c.collation,
        )

    def _constraint(self, table: str, c: ConstraintSnapshot) -> ConstraintSnapshot:
        return replace(
            c,
            name=self._object(_KIND_PREFIX.get(c.kind, "con"), f"{table}.{c.name}"),
            columns=[self.column(table, x) for x in c.columns],
            definition=self.text(c.definition, table),
        )

    def _relation(self, r: RelationSnapshot) -> RelationSnapshot:
        src = f"{r.from_schema}.{r.from_table}"
        dst = f"{r.to_schema}.{r.to_table}"
        fs, ft = self.table_map.get(src, (r.from_schema, r.from_table))
        ts, tt = self.table_map.get(dst, (r.to_schema, r.to_table))
        return replace(
            r,
            name=self._object("fk", f"{src}.{r.name}"),
            from_schema=fs,
            from_table=ft,
            from_columns=[self.column(src, x) for x in r.from_columns],
            to_schema=ts,
            to_table=tt,
            to_columns=[self.column(dst, x) for x in r.to_columns],
        )

    def _index(self, table: str, i: IndexSnapshot) -> IndexSnapshot:
        return replace(
            i,
            name=self._object("ix", f"{table}.{i.name}"),
            columns=[{**c, "name": self.column(table, c["name"])} for c in i.columns],
            filter_definition=self.text(i.filter_definition, table),
        )

    # -- evidence ---------------------------------------------------------------------

    def evidence(self, source: StaticEvidence) -> StaticEvidence:
        tables: dict[str, TableEvidenceInput] = {}
        for real in self.table_map:
            ev = source.table_evidence(real)
            if ev is not None:
                blinded = self.table_evidence(real, ev)
                tables[blinded.table] = blinded
        return StaticEvidence(tables, profiling_available=source.profiling_available)

    def table_evidence(self, real: str, ev: TableEvidenceInput) -> TableEvidenceInput:
        col = lambda name: self.column(real, name)  # noqa: E731
        return TableEvidenceInput(
            table=self.table(real),
            profiling_available=ev.profiling_available,
            tests_pending=ev.tests_pending,
            columns={col(n): replace(s, column=col(n)) for n, s in ev.columns.items()},
            identities=[
                IdentityRelation(
                    expression=self._identity(real, i.expression),
                    columns=tuple(col(c) for c in i.columns),
                    holds_rate=i.holds_rate,
                    kind=i.kind,
                )
                for i in ev.identities
            ],
            aliases={
                col(n): [self._alias(a) for a in items] for n, items in ev.aliases.items()
            },
            tests={
                col(n): [self._test(real, t) for t in items] for n, items in ev.tests.items()
            },
            lineage={col(n): self._lineage(real, info) for n, info in ev.lineage.items()},
        )

    def _identity(self, table: str, expression: str) -> str:
        return self.text(expression, table) or expression

    def _alias(self, a: ViewAlias) -> ViewAlias:
        view = self.table(a.view) if a.view in self.table_map else a.view
        return ViewAlias(
            view=view,
            alias=self.column(a.view, a.alias),
            expression=self.text(a.expression),
        )

    def _test(self, table: str, t: VerificationResult) -> VerificationResult:
        kind, _, target = t.test_id.partition(":")
        if kind == "inc":
            parent, _, column = target.rpartition(".")
            test_id = (
                f"inc:{self.table(parent)}.{self.column(parent, column)}"
                if parent in self.table_map else t.test_id
            )
        else:
            test_id = f"{kind}:{self.column(table, target)}"
        return replace(t, test_id=test_id, detail=self.text(t.detail, table) or "")

    def _lineage(self, view: str, info: LineageInfo) -> LineageInfo:
        source = None
        if info.source:
            schema_table, _, column = info.source.rpartition(".")
            source = (
                f"{self.table(schema_table)}.{self.column(schema_table, column)}"
                if schema_table in self.table_map else self.text(info.source)
            )
        return LineageInfo(
            classification=info.classification,
            source=source,
            expression=self.text(info.expression),
            inputs=tuple(self.text(x) or x for x in info.inputs),
            detail=self.text(info.detail) or "",
        )

    # -- the world --------------------------------------------------------------------

    def world(self, evidence: StaticEvidence) -> World:
        tables = {self.table(real): real for real in self.table_map}
        columns = {
            (self.table(real), tok): col
            for real, mapping in self.column_map.items()
            for col, tok in mapping.items()
        }
        return World(self.snapshot(), self.evidence(evidence), True, tables, columns)


_KIND_PREFIX = {"primary_key": "pk", "unique": "uq", "check": "ck", "default": "df"}
_SQL_WORDS = frozenset(
    """as and or not null is in case when then else end true false like ilike between
    cast convert coalesce nullif isnull trim ltrim rtrim btrim upper lower substring left right
    len length to_date to_char to_number date time timestamp text varchar char nvarchar
    int integer bigint smallint numeric decimal float real money bit boolean value values
    select from where group by order having distinct count sum avg min max over partition
    interval extract year month day hour minute second now getdate sysdatetime
    sysutcdatetime current_timestamp current_date regclass with any all exists""".split()
)


def summaries_unchanged(a: ColumnProfileSummary, b: ColumnProfileSummary) -> bool:
    """Data values are never blinded: every field but the column name is equal."""
    return replace(a, column="") == replace(b, column="")
