"""Cross-column identities in sampled rows (Phase 3A).

A verified ``quantity × unit_price = line_total`` identifies three measures
without one meaningful name; ``ordered_at <= shipped_at`` identifies a lifecycle.
Both are found on the sample already in memory — no extra query.

Checked, for at most ``max_columns`` numeric columns of a table:

* ``c = a × b``, ``c = a + b``, ``c = a − b`` within a tolerance of one cent or
  1e-6 relative, on rows where all three are non-null;
* for temporal columns, ``a <= b`` where the two differ on at least 5 % of rows
  (equal columns say nothing).

A relation is reported when it holds on >= ``min_rate`` of at least
``min_rows`` rows. Candidates are screened on the first ``screen`` rows first,
so the combinatorics stay cheap.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from itertools import combinations, permutations

from gaia.profiling.summary import IdentityRelation

__all__ = ["find_identities"]


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float | Decimal):
        return float(v)
    return None


def _close(x: float, y: float) -> bool:
    return abs(x - y) <= max(0.01, 1e-6 * max(abs(x), abs(y)))


_OPS = {
    "×": lambda a, b: a * b,
    "+": lambda a, b: a + b,
    "−": lambda a, b: a - b,
}


def _rate(rows: Sequence[tuple], test) -> tuple[float, int]:
    hits = total = 0
    for row in rows:
        result = test(row)
        if result is None:
            continue
        total += 1
        hits += bool(result)
    return (hits / total if total else 0.0), total


def find_identities(
    columns: Sequence[str],
    rows: Sequence[Sequence],
    *,
    numeric: Sequence[str],
    temporal: Sequence[str],
    max_columns: int = 12,
    min_rows: int = 50,
    min_rate: float = 0.99,
    screen: int = 200,
) -> list[IdentityRelation]:
    index = {c: i for i, c in enumerate(columns)}
    nums = [c for c in numeric if c in index][:max_columns]
    temps = [c for c in temporal if c in index][:max_columns]
    found: list[IdentityRelation] = []
    head, body = rows[:screen], rows

    def arithmetic(a: str, b: str, c: str, op: str):
        ia, ib, ic, f = index[a], index[b], index[c], _OPS[op]

        def test(row):
            x, y, z = _num(row[ia]), _num(row[ib]), _num(row[ic])
            if x is None or y is None or z is None:
                return None
            return _close(f(x, y), z)

        return test

    seen: set[frozenset[str]] = set()
    for c in nums:
        for a, b in combinations([n for n in nums if n != c], 2):
            for op, pairs in (("×", ((a, b),)), ("+", ((a, b),)), ("−", ((a, b), (b, a)))):
                for x, y in pairs:
                    key = frozenset((x, y, c, op))
                    if key in seen:
                        continue
                    test = arithmetic(x, y, c, op)
                    rate, n = _rate(head, test)
                    if n < min(min_rows, len(head)) or rate < min_rate:
                        continue
                    rate, n = _rate(body, test)
                    if n >= min_rows and rate >= min_rate:
                        seen.add(key)
                        found.append(
                            IdentityRelation(
                                f"{c} = {x} {op} {y}", (x, y, c), round(rate, 4), "arithmetic"
                            )
                        )

    for a, b in permutations(temps, 2):
        ia, ib = index[a], index[b]

        def order(row, ia=ia, ib=ib):
            x, y = row[ia], row[ib]
            if not isinstance(x, date | datetime) or not isinstance(y, date | datetime):
                return None
            if type(x) is not type(y):
                x, y = str(x), str(y)
            return x <= y

        rate, n = _rate(body, order)
        differ, _ = _rate(
            body,
            lambda row, ia=ia, ib=ib: (
                None if row[ia] is None or row[ib] is None else row[ia] != row[ib]
            ),
        )
        if n >= min_rows and rate >= min_rate and differ >= 0.05:
            found.append(IdentityRelation(f"{a} <= {b}", (a, b), round(rate, 4), "temporal_order"))
    return found
