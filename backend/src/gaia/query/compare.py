"""Is a generated result the reference's answer? (Phase 4D, execution accuracy)

The SQL text is never compared — two correct queries rarely look alike. Results are:

* **Columns are matched by their values**, not their names: the model's aliases are
  its own, and extra columns (a label, a second measure) are allowed. Every
  reference column must be found, except that of the ``identify_by`` columns (sku,
  name, id — different ways to say which thing) one is enough.
* **Numbers are equal at the precision either side shows**: 39.03 equals 39.0297
  (rounded to two places), 0.03 does not equal 0.0234. Integers and decimals of the
  same value are equal; a date equals a timestamp at midnight.
* **Rows**: the same number; the same rows as a multiset — or as a sequence when the
  question asks for an order (``order_matters``), rows tied on ``rank_by`` being
  interchangeable within their group.
* **Ties at a cut**: "the ten most" has several right answers when the tenth and
  eleventh are equal. With ``rank_by``, the reference is also fetched beyond its
  limit (``extended``): rows strictly above the cut must all be there, the rest may
  be any rows tied at the cut.
"""

from __future__ import annotations

import bisect
import itertools
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = ["Comparison", "Table", "compare"]

_MAX_ASSIGNMENTS = 200_000


@dataclass(frozen=True)
class Table:
    columns: list[str]
    rows: list[tuple[Any, ...]]


@dataclass
class Comparison:
    match: bool
    reason: str
    mapping: dict[str, str] = field(default_factory=dict)  # reference column -> generated


# -- values -------------------------------------------------------------------------------------


def _number(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(repr(v))
    return None


def _places(d: Decimal) -> int:
    """Decimal places as shown (3.00 has two: the database said so)."""
    exponent = d.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _norm(v: Any) -> Any:
    """A comparable form: numbers stay Decimal, temporal values become ISO text."""
    if isinstance(v, datetime):
        if v.time() == time(0) and (v.tzinfo is None or v.utcoffset().total_seconds() == 0):
            return v.date().isoformat()
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    n = _number(v)
    if n is not None:
        return n
    if isinstance(v, str):
        text = v.strip()
        try:  # numbers that came back as text (a driver's choice, or ::text)
            if text and text.replace(".", "", 1).lstrip("-").isdigit():
                return Decimal(text)
        except InvalidOperation:
            pass
        return text
    return v


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        places = min(_places(a), _places(b), 6)
        return abs(a - b) <= Decimal(5) * Decimal(10) ** (-(places + 1)) + Decimal("1e-9")
    return a == b


class _Values:
    """The values of one reference column, for a fast "does this value occur?" test:
    exact for text, nearest neighbours under ``_equal`` for numbers."""

    def __init__(self, values: list[Any]) -> None:
        self.numbers = sorted(v for v in values if isinstance(v, Decimal))
        self.floats = [float(v) for v in self.numbers]
        self.other = Counter(v for v in values if not isinstance(v, Decimal))

    def __contains__(self, v: Any) -> bool:
        if not isinstance(v, Decimal):
            return v in self.other
        at = bisect.bisect_left(self.floats, float(v))
        return any(_equal(v, self.numbers[i])
                   for i in range(max(0, at - 2), min(len(self.numbers), at + 3)))


# -- rows ---------------------------------------------------------------------------------------


def _rows_equal(a: tuple, b: tuple) -> bool:
    return all(_equal(x, y) for x, y in zip(a, b, strict=True))


def _multiset_match(got: list[tuple], want: list[tuple]) -> bool:
    if len(got) != len(want):
        return False
    pool = list(want)
    for row in got:
        hit = next((i for i, w in enumerate(pool) if _rows_equal(row, w)), None)
        if hit is None:
            return False
        pool.pop(hit)
    return True


def _tie_groups(rows: list[tuple], rank_index: int | None) -> list[list[tuple]]:
    if rank_index is None:
        return [[r] for r in rows]
    groups: list[list[tuple]] = []
    for r in rows:
        if groups and _equal(groups[-1][-1][rank_index], r[rank_index]):
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


def _check_rows(got: list[tuple], ref: list[tuple], extended: list[tuple] | None,
                rank_index: int | None, ordered: bool) -> str | None:
    """None when ``got`` is a right answer, else why not (all rows projected alike)."""
    n = len(ref)
    if len(got) != n:
        return f"{len(got)} row(s), the reference has {n}"
    if n == 0:
        return None
    fixed, boundary = ref, []
    if rank_index is not None and extended is not None and len(extended) > n:
        cut = ref[-1][rank_index]
        fixed = [r for r in ref if not _equal(r[rank_index], cut)]
        boundary = [r for r in extended if _equal(r[rank_index], cut)]
    if boundary:
        rest = list(got)
        for f in fixed:
            hit = next((i for i, g in enumerate(rest) if _rows_equal(g, f)), None)
            if hit is None:
                return "a row above the cut is missing"
            rest.pop(hit)
        pool = list(boundary)
        for g in rest:
            hit = next((i for i, b in enumerate(pool) if _rows_equal(g, b)), None)
            if hit is None:
                return "a row at the cut is not one of the tied rows"
            pool.pop(hit)
    elif not _multiset_match(got, ref):
        return "the rows differ"
    if ordered:
        ref_groups = _tie_groups(fixed + (got[len(fixed):] if boundary else []), rank_index) \
            if boundary else _tie_groups(ref, rank_index)
        i = 0
        for group in ref_groups:
            if not _multiset_match(got[i:i + len(group)], group):
                return "the rows are in another order"
            i += len(group)
    return None


# -- columns ------------------------------------------------------------------------------------


def compare(reference: Table, generated: Table, *, identify_by: list[str] | None = None,
            rank_by: str | None = None, order_matters: bool = False,
            extended: list[tuple] | None = None) -> Comparison:
    """Does ``generated`` answer what ``reference`` answers?"""
    ref_rows = [tuple(_norm(v) for v in r) for r in reference.rows]
    got_rows = [tuple(_norm(v) for v in r) for r in generated.rows]
    ext_rows = [tuple(_norm(v) for v in r) for r in extended] if extended is not None else None
    ref_cols = [c.lower() for c in reference.columns]
    ids = [c.lower() for c in identify_by or [] if c.lower() in ref_cols]
    core = [c for c in ref_cols if c not in ids]
    options = [core + [i] for i in ids] if ids else [core]
    if len(got_rows) != len(ref_rows):
        return Comparison(False, f"{len(got_rows)} row(s), the reference has {len(ref_rows)}")
    universe = ext_rows if ext_rows is not None else ref_rows

    def candidates(ci: int) -> list[int]:
        values = _Values([r[ci] for r in universe])
        return [gj for gj in range(len(generated.columns))
                if all(r[gj] in values for r in got_rows)]

    last_reason = "no column of the result holds the reference's values"
    for wanted in options:
        idx = [ref_cols.index(c) for c in wanted]
        cands = [candidates(ci) for ci in idx]
        if any(not c for c in cands):
            missing = wanted[[not c for c in cands].index(True)]
            last_reason = f"no column of the result holds the values of '{missing}'"
            continue
        rank_pos = wanted.index(rank_by.lower()) if rank_by and rank_by.lower() in wanted \
            else None
        tried = 0
        for assignment in itertools.product(*cands):
            if len(set(assignment)) != len(assignment):
                continue
            tried += 1
            if tried > _MAX_ASSIGNMENTS:
                break
            got = [tuple(r[j] for j in assignment) for r in got_rows]
            ref = [tuple(r[i] for i in idx) for r in ref_rows]
            ext = [tuple(r[i] for i in idx) for r in ext_rows] if ext_rows is not None \
                else None
            why = _check_rows(got, ref, ext, rank_pos, order_matters)
            if why is None:
                mapping = {reference.columns[i]: generated.columns[j]
                           for i, j in zip(idx, assignment, strict=True)}
                return Comparison(True, "same answer", mapping)
            last_reason = why
    return Comparison(False, last_reason)
