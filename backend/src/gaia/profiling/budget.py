"""Profiling budgets and policies (Phase 3A, P3-5 / P3-6).

* **Budgets** are in pages (8 KB) per table and per run, plus a time ceiling per
  table. A query is only started when its estimated cost fits; otherwise that
  one metric degrades (sampled, estimated or skipped) and says why. Nothing here
  ever fails a table or a run.
* **Size classes** decide the strategy: ``small`` (everything exact), ``large``
  (exact batched scan, sampled values, exact top-k for low-cardinality columns),
  ``huge`` (beyond ``scan_max_pages``: even the batched scan is replaced by the
  sample), ``columnstore`` (aggregates are cheap: treated like ``large``).
* **Run window**: profiling may be restricted to hours after the target's
  statistics maintenance (A5).
* **Freshness** (P3-6): a profile is reused unless the structure changed, the
  row count moved beyond ``row_count_delta_ratio``, or it is older than
  ``profile_max_age_days``. The data hash plays no part: it includes the latest
  statistics date, which Gaia's own queries move on SQL Server.
* **Catalog sufficiency**: a table whose every column already carries strong
  catalog evidence needs no profile.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dtime

from gaia.core.config import ProfilingBudget
from gaia.introspect.snapshot import TableSnapshot
from gaia.semantics.bundle import build_bundle

__all__ = [
    "RunBudget",
    "TableBudget",
    "catalog_sufficient",
    "freshness",
    "in_window",
    "size_class",
    "table_pages",
]

PAGE = 8192
ADAPTIVE_MIN_S = 5.0  # D50: the shortest plan for any statement


def table_pages(table: TableSnapshot) -> int:
    if table.size_bytes:
        return max(1, math.ceil(table.size_bytes / PAGE))
    if table.estimated_rows:
        return max(1, math.ceil(table.estimated_rows / 100))  # ~80 bytes/row guess
    return 1


def size_class(table: TableSnapshot, budget: ProfilingBudget, columnstore: bool) -> str:
    pages = table_pages(table)
    if pages <= budget.full_scan_max_pages:
        return "small"
    if columnstore:
        return "columnstore"
    if pages <= budget.scan_max_pages:
        return "large"
    return "huge"


@dataclass
class RunBudget:
    max_pages: int
    pages: int = 0

    def remaining(self) -> int:
        return max(0, self.max_pages - self.pages)


@dataclass
class TableBudget:
    max_pages: int
    max_seconds: float
    run: RunBudget
    # A statement may run this long before the server cancels it. A statement
    # is only started if it would end, even at its timeout, within the ceiling,
    # so ``max_seconds`` is a hard limit and not a point after which the last
    # query may still run for a minute.
    statement_timeout_s: float = 0.0
    started: float = field(default_factory=time.perf_counter)
    pages: int = 0
    # D50: "adaptive" plans with what this table's statements actually took;
    # "strict" with the full statement timeout.
    time_rule: str = "strict"
    slowest_s: float | None = None

    @property
    def elapsed_s(self) -> float:
        return time.perf_counter() - self.started

    def observe(self, seconds: float) -> None:
        """A statement on this table took ``seconds`` (a timeout counts in full)."""
        self.slowest_s = seconds if self.slowest_s is None else max(self.slowest_s, seconds)

    def expected_s(self) -> float:
        """How long the next statement is planned to take."""
        if self.time_rule != "adaptive" or self.slowest_s is None:
            return self.statement_timeout_s
        return min(self.statement_timeout_s, max(ADAPTIVE_MIN_S, 2 * self.slowest_s))

    def refusal(self, cost_pages: int) -> str | None:
        """Why a query of this cost may not start, or None when it may."""
        if self.elapsed_s >= self.max_seconds:
            return f"time budget ({self.max_seconds:.0f} s per table) used up"
        expected = self.expected_s()
        if self.elapsed_s + expected > self.max_seconds:
            if expected < self.statement_timeout_s:
                return (
                    f"time budget: a statement started now would likely run past the "
                    f"{self.max_seconds:.0f} s ceiling (planned {expected:.0f} s: twice the "
                    f"slowest so far)"
                )
            return (
                f"time budget: a statement started now could run past the "
                f"{self.max_seconds:.0f} s ceiling (statement timeout "
                f"{self.statement_timeout_s:.0f} s)"
            )
        if self.pages + cost_pages > self.max_pages:
            return f"page budget ({self.max_pages:,} per table) would be exceeded"
        if self.run.pages + cost_pages > self.run.max_pages:
            return f"run page budget ({self.run.max_pages:,}) would be exceeded"
        return None

    def spend(self, pages: int) -> None:
        self.pages += pages
        self.run.pages += pages


def _clock(text: str) -> dtime:
    hours, minutes = text.strip().split(":")
    return dtime(int(hours), int(minutes))


def in_window(window: str | None, now: datetime | None = None) -> bool:
    """``"22:00-06:00"`` (may wrap midnight); None = always."""
    if not window:
        return True
    start_text, end_text = window.split("-", 1)
    start, end = _clock(start_text), _clock(end_text)
    current = (now or datetime.now()).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def freshness(
    *,
    profile_structure_hash: str | None,
    profile_rows: int | None,
    profiled_at: datetime | None,
    status: str | None,
    table: TableSnapshot,
    row_count_delta_ratio: float,
    max_age_days: int,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """(reusable, reason) for the current profile of a table."""
    if profile_structure_hash is None or profiled_at is None:
        return False, "no profile yet"
    if status not in ("profiled", "partial"):
        return False, f"last attempt was {status}"
    if profile_structure_hash != table.structure_hash:
        return False, "structure changed"
    now = now or datetime.now(UTC)
    if profiled_at.tzinfo is None:
        profiled_at = profiled_at.replace(tzinfo=UTC)
    if now - profiled_at > timedelta(days=max_age_days):
        return False, f"older than {max_age_days} days"
    before, after = profile_rows or 0, table.estimated_rows or 0
    if max(before, after) > 0:
        delta = abs(after - before) / max(before, 1)
        if delta > row_count_delta_ratio:
            return False, f"row count moved {delta:.0%} (from {before:,} to {after:,})"
    return True, "fresh"


def catalog_sufficient(table: TableSnapshot, dialect: str) -> bool:
    """Every column has a comment, a single-column key, a trusted FK or a trusted check."""
    bundle = build_bundle(table, dialect=dialect)
    if not bundle.facts:
        return False
    for facts in bundle.facts.values():
        strong = (
            facts.has_comment
            or (facts.in_primary_key and facts.primary_key_size == 1)
            or facts.in_unique
            or (facts.foreign_keys and facts.foreign_keys_trusted)
            or (facts.checks and facts.checks_trusted)
        )
        if not strong:
            return False
    return True
