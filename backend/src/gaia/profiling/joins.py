"""Join discovery and verification tests (Phase 3A, Stages 1 and 3).

**Stage 1 — candidates from data, names not required.** A column without a
declared foreign key is paired with single-column keys (PK or unique) of other
tables when

* the value families match (integer/integer, text/text, uuid/uuid);
* text: at least half of the sampled child rows carry values shaped like the
  parent's keys (``A999``) and no longer than the longest key;
* integers: the child's range lies inside the parent's, covers at least half of
  it, and the child has no more distinct values than the parent.

At most ``per_child`` parents are tested per column, ranked by range fit and,
when names carry meaning, by shared name tokens.

**The inclusion test** sends the child's most frequent sampled values (at most
``join_test_values``) to the parent key (an index seek) and learns which exist.
Its metric is **row-weighted**: the share of sampled child *rows* whose value
exists in the parent. Legacy data typically has orphans that are rare values
(2 % of rows, but half of the distinct codes), and a reference with 98 % valid
rows is still a reference. ``passed`` at >= 95 %; a Stage 1 result is kept as
evidence when it passed, or when at least half of the rows *and* half of the
distinct values were found.

**Specificity.** Inclusion proves little when a random value would be found
anyway: a dense integer key (ids 1..N with few gaps) contains *every* integer in
its range, so a quantity or a weight "references" it. For integers, the chance
that ``d`` random in-range values are all found is ``density ** d``, where
density = distinct keys / range width; the test is *specific* only when that
chance is below 1 %. Text and uuid values are specific. A non-specific pair is
not even a Stage 1 candidate (a pass would only invite the model to see a
reference in a quantity or a time); a non-specific Stage 3 test of a claimed join
is weak support for the reference and never contradicts another role
(``semantics.evidence``).

**Stage 3 — tests of claims** (``profiling.service.run_stage3``): an inclusion
test per join hint the model proposed, exact uniqueness for identifiers, exact
top-k for codes and flags whose counts were sampled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from gaia.dialects.types import type_info
from gaia.introspect.snapshot import Snapshot, TableSnapshot
from gaia.profiling.summary import ColumnProfileSummary, VerificationResult
from gaia.semantics.evidence import shape

__all__ = [
    "Inclusion",
    "JoinCandidate",
    "family",
    "find_candidates",
    "inclusion_test",
    "integer_chance",
    "single_keys",
    "topk_test",
    "uniqueness_test",
]

PASS_RATE = 0.95
KEEP_RATE = 0.50
CHANCE_MAX = 0.01
_TOKEN = re.compile(r"[a-z]+")
_INTEGER = {"tinyint", "smallint", "int", "integer", "bigint", "int2", "int4", "int8"}

Weighted = list[tuple[Any, int]]  # (value, rows in the sample), most frequent first


@dataclass(frozen=True)
class JoinCandidate:
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    score: float
    reason: str
    specific: bool = True  # False: a pass could be chance (dense integer key)


def single_keys(table: TableSnapshot) -> set[str]:
    keys: set[str] = set()
    for con in table.constraints:
        if con.kind in ("primary_key", "unique") and len(con.columns) == 1:
            keys.update(con.columns)
    for idx in table.indexes:
        cols = [c["name"] for c in idx.columns if not c.get("included")]
        if idx.is_unique and not idx.filter_definition and not idx.is_disabled and len(cols) == 1:
            keys.update(cols)
    return keys


def family(dialect: str, data_type: str) -> str | None:
    """integer | text | uuid: the value families join discovery compares."""
    info = type_info(dialect, data_type)
    if info.is_array:
        return None
    if info.base in _INTEGER:
        return "integer"
    if info.is_text:
        return "text"
    if info.is_uuid:
        return "uuid"
    return None


def _shapes(values: list[Any]) -> set[str]:
    return {shape(str(v).strip()) for v in values if v is not None}


def _number(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def _tokens(*names: str) -> set[str]:
    out: set[str] = set()
    for name in names:
        out.update(t for t in _TOKEN.findall(name.lower()) if len(t) > 2)
    return out


def integer_chance(parent: ColumnProfileSummary | None, tested_values: int) -> float:
    """Chance that ``tested_values`` random integers in the parent's range all exist."""
    if parent is None or parent.distinct is None:
        return 1.0
    low, high = _number(parent.minimum), _number(parent.maximum)
    if low is None or high is None or high < low:
        return 1.0
    density = min(1.0, parent.distinct / (high - low + 1))
    return density ** max(1, tested_values)


def find_candidates(
    snapshot: Snapshot,
    summaries: dict[str, dict[str, ColumnProfileSummary]],
    child_values: dict[tuple[str, str], Weighted],
    parent_values: dict[tuple[str, str], Weighted],
    *,
    per_child: int = 2,
) -> list[JoinCandidate]:
    dialect = snapshot.dialect
    tables = {t.qualified_name: t for t in snapshot.tables if t.kind == "table"}
    fk_columns = {
        (t.qualified_name, c) for t in tables.values() for r in t.relations for c in r.from_columns
    }
    parents: list[tuple[str, str, str]] = []  # table, column, family
    for t in tables.values():
        types = {c.name: c.data_type for c in t.columns}
        for key in sorted(single_keys(t)):
            fam = family(dialect, types.get(key, ""))
            if fam:
                parents.append((t.qualified_name, key, fam))

    out: list[JoinCandidate] = []
    for (ct, cc), weighted in sorted(child_values.items()):
        table = tables.get(ct)
        if table is None or (ct, cc) in fk_columns or cc in single_keys(table):
            continue
        col = next((c for c in table.columns if c.name == cc), None)
        fam = family(dialect, col.data_type) if col else None
        cs = summaries.get(ct, {}).get(cc)
        values = [v for v, _n in weighted]
        if fam is None or cs is None or not values or (cs.distinct or 0) < 2:
            continue
        ranked: list[JoinCandidate] = []
        for pt, pc, pfam in parents:
            if pfam != fam or (pt == ct and pc == cc):
                continue
            ps = summaries.get(pt, {}).get(pc)
            specific = True
            if fam == "text":
                # Most rows must carry values shaped like the parent's keys;
                # the rest may be orphans or point elsewhere (polymorphic refs).
                pvals = [v for v, _n in parent_values.get((pt, pc), [])] or (
                    [ps.minimum, ps.maximum, *ps.sample_values] if ps else []
                )
                parent_shapes = _shapes([v for v in pvals if v])
                if not parent_shapes:
                    continue
                max_len = ps.max_length if ps and ps.max_length is not None else None
                rows = sum(n for _v, n in weighted) or 1
                fitting = sum(
                    n
                    for v, n in weighted
                    if shape(str(v).strip()) in parent_shapes
                    and (max_len is None or len(str(v).strip()) <= max_len)
                )
                fit = fitting / rows
                if fit < KEEP_RATE:
                    continue
            elif fam == "integer":
                if ps is None:
                    continue
                cmin, cmax = _number(cs.minimum), _number(cs.maximum)
                pmin, pmax = _number(ps.minimum), _number(ps.maximum)
                if None in (cmin, cmax, pmin, pmax) or cmin < pmin or cmax > pmax or pmax <= 0:
                    continue
                fit = cmax / pmax
                if fit < 0.5 or (ps.distinct is not None and (cs.distinct or 0) > ps.distinct):
                    continue
                specific = integer_chance(ps, len(values)) < CHANCE_MAX
                if not specific:
                    continue  # a dense key contains every in-range integer: untestable
            else:
                fit = 1.0
            names = len(_tokens(cc) & _tokens(pt.split(".")[1], pc))
            ranked.append(
                JoinCandidate(
                    ct,
                    cc,
                    pt,
                    pc,
                    round(fit + 0.5 * names, 4),
                    f"{fam} values; fit {fit:.2f}"
                    + (f"; {names} shared name token(s)" if names else ""),
                    specific,
                )
            )
        ranked.sort(key=lambda c: (-c.score, c.parent_table, c.parent_column))
        out.extend(ranked[:per_child])
    return out


@dataclass(frozen=True)
class Inclusion:
    result: VerificationResult
    distinct_rate: float  # share of the tested distinct values found

    @property
    def worth_keeping(self) -> bool:
        """Stage 1 keeps a specific result that passed, or that found most rows
        *and* most distinct values (one heavy value that happens to exist in
        the parent is a coincidence). A non-specific result carries no
        information — every in-range integer is found in a dense key — and is
        only listed in the run report."""
        r = self.result
        if not r.specific:
            return False
        return r.passed or (
            r.metric is not None and r.metric >= KEEP_RATE and self.distinct_rate >= KEEP_RATE
        )


def inclusion_test(
    profiler,
    cur,
    parent_table: str,
    parent_column: str,
    weighted: Weighted,
    *,
    stage: int,
    origin: str,
    specific: bool = True,
    parent_type: str | None = None,
) -> Inclusion:
    """Row-weighted share of the child's sampled values that exist in the parent key."""
    test_id = f"inc:{parent_table}.{parent_column}"
    counts: dict[Any, int] = {}
    for value, n in weighted:
        if value is not None:
            counts[value] = counts.get(value, 0) + int(n)
    if not counts:
        return Inclusion(
            VerificationResult(
                test_id, "inclusion", False, None, "no non-null values to test", origin, specific
            ),
            0.0,
        )
    schema, name = parent_table.split(".", 1)
    sql, params = profiler.inclusion_sql(
        profiler.table(schema, name), parent_column, list(counts), parent_type
    )
    found = {_key(r[0]) for r in profiler.query(cur, sql, params)}
    hit = {v for v in counts if _key(v) in found}
    rows = sum(counts.values())
    hit_rows = sum(counts[v] for v in hit)
    rate = hit_rows / rows
    missing = len(counts) - len(hit)
    detail = (
        f"{rate:.1%} of sampled rows ({len(hit)} of {len(counts)} distinct values) found in "
        f"{parent_table}.{parent_column} (stage {stage})"
    )
    if missing and rate >= KEEP_RATE:
        detail += f"; {missing} value(s) not found: orphans or another parent"
    if not specific:
        detail += "; the parent key is dense, so any in-range integer matches: weak evidence"
    result = VerificationResult(
        test_id, "inclusion", rate >= PASS_RATE, round(rate, 4), detail, origin, specific
    )
    return Inclusion(result, len(hit) / len(counts))


def _key(value: Any) -> str:
    """Compare values across drivers' types (char padding, Decimal vs int, uuid)."""
    return str(value).strip()


def uniqueness_test(profiler, cur, table: TableSnapshot, column: str) -> VerificationResult:
    t = profiler.table(table.schema, table.name)
    distinct = int(profiler.query(cur, profiler.uniqueness_sql(t, column))[0][0])
    non_null = int(profiler.query(cur, profiler.non_null_sql(t, column))[0][0])
    return VerificationResult(
        test_id=f"uniq:{column}",
        kind="uniqueness",
        passed=non_null > 0 and distinct == non_null,
        metric=round(distinct / non_null, 6) if non_null else None,
        detail=f"{distinct:,} distinct of {non_null:,} non-null values (exact, stage 3)",
        origin="claim",
    )


def topk_test(
    profiler, cur, table: TableSnapshot, column: str, role: str, low_cardinality_max: int
) -> tuple[VerificationResult, list[tuple[str, int]]]:
    """Exact distinct count of a claimed code or flag (one ``GROUP BY``)."""
    t = profiler.table(table.schema, table.name)
    limit = low_cardinality_max + 1
    rows = profiler.query(cur, profiler.topk_sql(t, column, limit))
    distinct = len(rows)
    allowed = 3 if role == "flag" else low_cardinality_max
    more = "+" if distinct >= limit else ""
    return VerificationResult(
        test_id=f"topk:{column}",
        kind="topk_exact",
        passed=distinct <= allowed,
        metric=float(distinct),
        detail=f"{distinct:,}{more} distinct values (exact); a {role} allows at most {allowed}",
        origin="claim",
    ), [(str(v), int(c)) for v, c in rows[:10]]
