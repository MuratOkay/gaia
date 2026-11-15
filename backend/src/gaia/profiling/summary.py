"""The evidence contract between profiling (3A) and reasoning (3B).

Phase 3B reasons with evidence; Phase 3A produces it. This module is the
boundary: 3A's profiler fills these dataclasses, 3B renders them into the
prompt, scores against them and hashes them. Nothing here touches a database.

Evidence sources:

* the profile store (``ProfileStore.load_evidence``, the default when profiling
  is enabled for the target) — a ``StaticEvidence`` built from stored profiles,
  tests and view lineage;
* ``NoEvidence`` — catalog only (the degraded mode of PHASES Stage 1);
* ``StaticEvidence`` — in-memory, for tests;
* ``JsonFileEvidence`` — a JSON document (format: ``to_dict``), for fixtures and
  experiments (``make reason ARGS="--evidence-file F"``).

Values are strings on purpose: profiles of every dialect and type travel
through the same shape, and rendering (rounding, masking) happens in one place.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "ColumnProfileSummary",
    "LineageInfo",
    "DetectorHit",
    "EvidenceSource",
    "IdentityRelation",
    "JsonFileEvidence",
    "NoEvidence",
    "StaticEvidence",
    "TableEvidenceInput",
    "VerificationResult",
    "ViewAlias",
]


@dataclass(frozen=True)
class DetectorHit:
    """A semantic-type detector's verdict on a column's sampled values."""

    semantic_type: str  # vocabulary: gaia.semantics.evidence.SEMANTIC_TYPES
    match_rate: float  # share of non-null sampled values that validated, 0..1
    sample_size: int = 0


@dataclass(frozen=True)
class IdentityRelation:
    """A cross-column identity verified on sampled rows, e.g. f7 = f5 * f6."""

    expression: str  # human-readable, e.g. "f7 = f5 × f6"
    columns: tuple[str, ...]  # every column taking part
    holds_rate: float  # share of sampled rows where it holds, 0..1
    kind: str = "arithmetic"  # arithmetic | temporal_order


@dataclass(frozen=True)
class ViewAlias:
    """A view exposes this column under another name (A8): passed through, or
    only converted (cast, ``to_date``, ``trim``)."""

    view: str  # schema.view
    alias: str
    # Set when the view converts the column rather than passing it through,
    # e.g. "TO_DATE(CAST(t.f3 AS TEXT), 'YYYYMMDD')".
    expression: str | None = None


@dataclass(frozen=True)
class LineageInfo:
    """Where a *view* column comes from (3A lineage): pass-through or computed."""

    classification: str  # pass_through | computed | unresolved
    source: str | None = None  # schema.table.column
    expression: str | None = None
    inputs: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class VerificationResult:
    """A test on the data: a Stage 1 join candidate or a Stage 3 check of a claim."""

    test_id: str
    kind: str  # inclusion | uniqueness | topk_exact | range | parse
    passed: bool
    metric: float | None = None  # e.g. inclusion rate
    detail: str = ""
    # candidate: found by Stage 1 without a claim (a failure says nothing about
    # the column's meaning); claim: tests what the model said (Stage 3).
    origin: str = "claim"
    # False when passing is likely by chance (small dense integers fit any
    # dense integer key): such a pass is weak support and never a contradiction.
    specific: bool = True
    # False when the test could not run (budget, timeout, a vanished parent):
    # it is recorded so the claim is not escalated forever, but it is not
    # evidence — not shown to the model, not in the ledger (stored as method
    # ``not_run``).
    conclusive: bool = True


@dataclass
class ColumnProfileSummary:
    column: str
    method: str = "exact"  # exact | sampled_pages | histogram | approximate
    rows: int | None = None
    non_null: int | None = None
    distinct: int | None = None
    distinct_method: str | None = None  # exact | approximate | sampled | estimate
    minimum: str | None = None
    maximum: str | None = None
    top_values: list[tuple[str, int]] = field(default_factory=list)  # value, row count
    min_length: int | None = None
    max_length: int | None = None
    avg_length: float | None = None
    numeric_parse_rate: float | None = None  # text columns only
    date_parse_rate: float | None = None  # text / integer columns only
    sample_values: list[str] = field(default_factory=list)
    detectors: list[DetectorHit] = field(default_factory=list)
    # temporal columns: year | month | day | minute | second | sub-second
    granularity: str | None = None
    # text columns: blank values ('' or spaces) counted as NULL, i.e. included
    # in the null fraction (owner decision 2026-11-15); None = not counted
    empty: int | None = None

    @property
    def null_fraction(self) -> float | None:
        if self.rows is None or self.non_null is None or self.rows <= 0:
            return None
        return max(0.0, 1.0 - self.non_null / self.rows)

    @property
    def distinct_ratio(self) -> float | None:
        if self.distinct is None or not self.non_null:
            return None
        return min(1.0, self.distinct / self.non_null)

    @property
    def is_empty(self) -> bool:
        return self.non_null == 0

    @property
    def is_constant(self) -> bool:
        return bool(self.non_null) and self.distinct == 1


@dataclass
class TableEvidenceInput:
    table: str  # schema.table
    # Can data be profiled on this target at all (SELECT granted, profiling
    # enabled)? False means catalog-only is the deepest coverage available.
    profiling_available: bool = False
    # Stage 3 tests that apply to this table but have not run yet.
    tests_pending: bool = False
    columns: dict[str, ColumnProfileSummary] = field(default_factory=dict)
    identities: list[IdentityRelation] = field(default_factory=list)
    aliases: dict[str, list[ViewAlias]] = field(default_factory=dict)  # column -> aliases
    tests: dict[str, list[VerificationResult]] = field(default_factory=dict)  # column -> tests
    lineage: dict[str, LineageInfo] = field(default_factory=dict)  # view column -> lineage

    @property
    def profiled(self) -> bool:
        return bool(self.columns)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TableEvidenceInput:
        columns = {}
        for name, c in (data.get("columns") or {}).items():
            c = dict(c)
            c["top_values"] = [tuple(v) for v in c.get("top_values") or []]
            c["detectors"] = [DetectorHit(**d) for d in c.get("detectors") or []]
            columns[name] = ColumnProfileSummary(**{"column": name, **c})
        return cls(
            table=data["table"],
            profiling_available=bool(data.get("profiling_available", bool(columns))),
            tests_pending=bool(data.get("tests_pending", False)),
            columns=columns,
            identities=[
                IdentityRelation(**{**i, "columns": tuple(i["columns"])})
                for i in data.get("identities") or []
            ],
            aliases={
                col: [ViewAlias(**a) for a in items]
                for col, items in (data.get("aliases") or {}).items()
            },
            tests={
                col: [VerificationResult(**t) for t in items]
                for col, items in (data.get("tests") or {}).items()
            },
            lineage={
                col: LineageInfo(**{**info, "inputs": tuple(info.get("inputs") or ())})
                for col, info in (data.get("lineage") or {}).items()
            },
        )


class EvidenceSource(Protocol):
    def table_evidence(self, qualified_name: str) -> TableEvidenceInput | None:
        """Evidence for one table, or ``None`` when nothing beyond the catalog exists."""
        ...

    @property
    def profiling_available(self) -> bool: ...


class NoEvidence:
    """Catalog only. Profiling is not available, so catalog coverage is final."""

    profiling_available = False

    def table_evidence(self, qualified_name: str) -> TableEvidenceInput | None:
        return None


class StaticEvidence:
    def __init__(
        self, tables: dict[str, TableEvidenceInput], *, profiling_available: bool = True
    ) -> None:
        self.tables = {k.lower(): v for k, v in tables.items()}
        self.profiling_available = profiling_available

    def table_evidence(self, qualified_name: str) -> TableEvidenceInput | None:
        return self.tables.get(qualified_name.lower())


class JsonFileEvidence(StaticEvidence):
    """``{"profiling_available": true, "tables": [TableEvidenceInput.to_dict(), ...]}``."""

    def __init__(self, path: str | Path) -> None:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        tables = {t["table"]: TableEvidenceInput.from_dict(t) for t in doc.get("tables", [])}
        super().__init__(tables, profiling_available=bool(doc.get("profiling_available", True)))
