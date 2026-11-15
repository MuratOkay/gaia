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
its range, so a quantity or a weight "references" it. Under that null
hypothesis each of ``d`` tested values is found with probability ``density``
(distinct keys / range width), so the number found is binomial. Two checks:

* *before* a Stage 1 test: the pair is a candidate only if even the best
  outcome could be specific, i.e. ``density ** d`` (all ``d`` found) < 1 %;
* *after* any integer test: the result is specific only if finding at least
  that many values by chance is < 1 % (the binomial upper tail), with the
  density measured where the tested values lie (one index range count on the
  parent key), not over the key's whole range.

The second check was added after the owner's SQL Server run (2026-11-15). The
first alone is not enough, because a test passes at 95 % and is kept at 50 %,
not at 100 %: random integers against a 97 %-dense key are all found with 0.2 %
probability, yet ~97 % of them are found every time, so a count "passed" into a
payroll line id (``agg_product_performance.line_count``, 99 %).

Text and uuid values are specific. A non-specific pair is not a Stage 1
candidate, and a non-specific result is not kept (it would only invite the
model to see a reference in a quantity or a time); a non-specific Stage 3 test
of a claimed join is weak support for the reference and never contradicts
another role (``semantics.evidence``).

**Types.** A claimed join may pair a text column with an integer key (the
model aimed ``f2`` with values like ``C00006638`` at ``customers.customer_id``).
Values are converted to the parent's family before they are sent: text that
is an integer is tested as an integer, anything else cannot be in the key and
counts as not found. Such a test *fails* — the claim is wrong — instead of
raising a conversion error, which had left the claim untested forever.

**Stage 3 — tests of claims** (``profiling.service.run_stage3``): an inclusion
test per join hint the model proposed, exact uniqueness for identifiers, exact
top-k for codes and flags whose counts were sampled.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
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
    "chance_of_hits",
    "coerce_values",
    "integer_chance",
    "integer_density",
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


def integer_density(parent: ColumnProfileSummary | None) -> float | None:
    """Share of the integers in the parent key's range that exist (None: unknown)."""
    if parent is None or parent.distinct is None:
        return None
    low, high = _number(parent.minimum), _number(parent.maximum)
    if low is None or high is None or high < low:
        return None
    return min(1.0, parent.distinct / (high - low + 1))


def integer_chance(parent: ColumnProfileSummary | None, tested_values: int) -> float:
    """Chance that ``tested_values`` random integers in the parent's range all exist
    (the best outcome a test could have; unknown density counts as 1)."""
    density = integer_density(parent)
    if density is None:
        return 1.0
    return density ** max(1, tested_values)


def chance_of_hits(hits: int, tested: int, density: float) -> float:
    """P(X >= hits) for X ~ Binomial(tested, density): the chance that at least
    ``hits`` of ``tested`` random in-range integers exist in the key."""
    if hits <= 0:
        return 1.0
    if density >= 1.0:
        return 1.0
    if density <= 0.0:
        return 0.0
    log_p, log_q = math.log(density), math.log1p(-density)
    total = 0.0
    for k in range(hits, tested + 1):
        log_term = (
            math.lgamma(tested + 1) - math.lgamma(k + 1) - math.lgamma(tested - k + 1)
            + k * log_p + (tested - k) * log_q
        )
        total += math.exp(log_term)
    return min(1.0, total)


def find_candidates(
    snapshot: Snapshot,
    summaries: dict[str, dict[str, ColumnProfileSummary]],
    child_values: dict[tuple[str, str], Weighted],
    parent_values: dict[tuple[str, str], Weighted],
    *,
    per_child: int = 2,
    parents_in: set[str] | None = None,
) -> list[JoinCandidate]:
    """``parents_in``: only these parent tables (a reused child against the
    tables profiled in this run)."""
    dialect = snapshot.dialect
    tables = {t.qualified_name: t for t in snapshot.tables if t.kind == "table"}
    fk_columns = {
        (t.qualified_name, c) for t in tables.values() for r in t.relations for c in r.from_columns
    }
    parents: list[tuple[str, str, str]] = []  # table, column, family
    for t in tables.values():
        types = {c.name: c.data_type for c in t.columns}
        if parents_in is not None and t.qualified_name not in parents_in:
            continue
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


_INT_TEXT = re.compile(r"^\s*[+-]?\d+\s*$")


def coerce_values(values: list[Any], parent_family: str | None) -> dict[Any, Any]:
    """Each child value as the parent key's family would hold it; values that
    cannot be in such a key (text that is not an integer, not a uuid) are left
    out — they count as *not found*."""
    out: dict[Any, Any] = {}
    for v in values:
        if v is None:
            continue
        if parent_family == "integer":
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                out[v] = v
            elif isinstance(v, float | Decimal):
                if v == int(v):
                    out[v] = int(v)
            elif _INT_TEXT.match(str(v)):
                out[v] = int(str(v).strip())
        elif parent_family == "uuid":
            try:
                out[v] = str(uuid.UUID(str(v).strip()))
            except ValueError:
                continue
        elif parent_family == "text":
            out[v] = v if isinstance(v, str) else str(v)
        else:
            out[v] = v
    return out


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
    child_type: str | None = None,
    density: float | None = None,
) -> Inclusion:
    """Row-weighted share of the child's sampled values that exist in the parent key.

    ``density`` (integer keys) makes the result's specificity an outcome of the
    test: a pass or keepable share that chance alone would reach at least 1 %
    of the time is marked non-specific (module docstring).
    """
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
    dialect = getattr(profiler, "dialect", "mssql")
    parent_family = family(dialect, parent_type) if parent_type else None
    child_family = family(dialect, child_type) if child_type else None
    mismatch = parent_family is not None and child_family != parent_family
    sendable = coerce_values(list(counts), parent_family) if parent_family else {
        v: v for v in counts
    }
    found: set[str] = set()
    if sendable:
        schema, name = parent_table.split(".", 1)
        sql, params = profiler.inclusion_sql(
            profiler.table(schema, name), parent_column, list(dict.fromkeys(sendable.values())),
            parent_type,
        )
        found = {_key(r[0]) for r in profiler.query(cur, sql, params)}
    hit = {v for v, sent in sendable.items() if _key(sent) in found}
    rows = sum(counts.values())
    hit_rows = sum(counts[v] for v in hit)
    rate = hit_rows / rows
    missing = len(counts) - len(hit)
    detail = (
        f"{rate:.1%} of sampled rows ({len(hit)} of {len(counts)} distinct values) found in "
        f"{parent_table}.{parent_column} (stage {stage})"
    )
    if mismatch:
        unusable = len(counts) - len(sendable)
        detail += (
            f"; the key is {parent_family or 'another type'}, the column "
            f"{child_family or 'another type'}"
            + (f": {unusable} value(s) cannot be {parent_family} keys" if unusable else "")
        )
    if missing and rate >= KEEP_RATE:
        detail += f"; {missing} value(s) not found: orphans or another parent"
    if parent_family == "integer" and rate >= KEEP_RATE and (
        density is not None or hasattr(profiler, "range_count_sql")
    ):
        local = _local_density(profiler, cur, parent_table, parent_column, sendable)
        effective = local if local is not None else density
        if effective is not None and chance_of_hits(len(hit), len(counts), effective) >= (
            CHANCE_MAX
        ):
            specific = False
    if not specific:
        detail += "; the parent key is dense, so any in-range integer matches: weak evidence"
    result = VerificationResult(
        test_id, "inclusion", rate >= PASS_RATE, round(rate, 4), detail, origin, specific
    )
    return Inclusion(result, len(hit) / len(counts))


def _local_density(profiler, cur, parent_table: str, parent_column: str,
                   sendable: dict[Any, Any]) -> float | None:
    """Density of the parent key *where the tested values lie*: keys between the
    smallest and largest tested value / width of that range (one index range
    count). The key's overall density misleads when ids are dense in one range
    and sparse elsewhere (owner run 2026-11-15: a count still "passed" into
    hr.payroll_lines.payroll_line_id at 99 % after the binomial check, because
    the ids are dense where the counts lie)."""
    if not hasattr(profiler, "range_count_sql"):
        return None
    ints = [v for v in sendable.values() if isinstance(v, int) and not isinstance(v, bool)]
    if not ints:
        return None
    low, high = min(ints), max(ints)
    schema, name = parent_table.split(".", 1)
    sql, params = profiler.range_count_sql(profiler.table(schema, name), parent_column, low, high)
    try:
        keys = int(profiler.query(cur, sql, params)[0][0])
    except Exception:  # the global density still applies
        return None
    return min(1.0, keys / (high - low + 1))


def _key(value: Any) -> str:
    """Compare values across drivers' types (char padding, Decimal vs int, uuid)."""
    return str(value).strip()


def uniqueness_test(profiler, cur, table: TableSnapshot, column: str) -> VerificationResult:
    t = profiler.table(table.schema, table.name)
    data_type = next((c.data_type for c in table.columns if c.name == column), None)
    distinct = int(profiler.query(cur, profiler.uniqueness_sql(t, column, data_type))[0][0])
    non_null = int(profiler.query(cur, profiler.non_null_sql(t, column, data_type))[0][0])
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
    data_type = next((c.data_type for c in table.columns if c.name == column), None)
    rows = profiler.query(cur, profiler.topk_sql(t, column, limit, data_type))
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
