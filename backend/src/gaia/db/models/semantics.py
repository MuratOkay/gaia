"""Semantic models: versioned meaning, scores, join hints, glossary, embeddings.

Table names follow PHASES (``table_semantic``, ``column_semantic``,
``join_hint``, ``glossary_term``, ``embedding``). None of them is a reserved
word, unlike Phase 1's ``table``/``column``/``constraint``.

Versioning (OUTLINE §8): results are never overwritten. Each reasoning of a
table inserts a new ``version`` for the table and all of its columns; the
previous rows stay with ``is_current = false``. A partial unique index keeps
exactly one current row per table and per column. ``semantics.reasoning_run``
records each run, so query runs can pin a semantic version (Phase 4).

PII fields exist from day one (OUTLINE §3) but stay NULL until Phase 3.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from gaia.db.models.base import Base
from gaia.db.models.catalog import new_uuid

# Baked into the column type (OUTLINE §15). Changing the embedding model's
# dimensionality is an explicit migration plus re-embed, never silent.
EMBEDDING_DIMENSIONS = 1024

SEMANTIC_STATUSES = ("draft", "auto_approved", "sme_reviewed", "sme_rejected", "stale")
SCORE_BANDS = ("accepted", "deep_profile", "deep_profile_flagged", "abstain")
COVERAGE_LEVELS = ("catalog", "modules", "light_profile", "targeted", "sme")
REVIEW_STATUSES = ("open", "done", "dismissed", "superseded")


def _status_check() -> sa.CheckConstraint:
    allowed = ", ".join(f"'{s}'" for s in SEMANTIC_STATUSES)
    return sa.CheckConstraint(f"status IN ({allowed})", name="status")


class ReasoningRun(Base):
    """One execution of the reasoning pipeline over a scope of tables."""

    __tablename__ = "reasoning_run"
    __table_args__ = (
        sa.Index("ix_reasoning_run_source_started", "data_source_id", "started_at"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'completed_with_failures', 'failed')",
            name="status",
        ),
        {"schema": "semantics"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.snapshots.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    critique_prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    thinking: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    critique_thinking: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    tables_in_scope: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tables_reasoned: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tables_skipped: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tables_failed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    critiques_failed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    columns_written: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    embeddings_written: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    llm_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cache_hits: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    wall_ms: Mapped[int | None] = mapped_column(sa.BigInteger)
    notes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


class TableSemantic(Base):
    """Versioned business meaning of a table or view."""

    __tablename__ = "table_semantic"
    __table_args__ = (
        sa.UniqueConstraint("table_id", "version", name="uq_table_semantic_table_version"),
        sa.Index(
            "uq_table_semantic_current",
            "table_id",
            unique=True,
            postgresql_where=sa.text("is_current"),
        ),
        sa.Index("ix_table_semantic_source_current", "data_source_id", "is_current"),
        _status_check(),
        {"schema": "semantics"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("semantics.reasoning_run.id", ondelete="SET NULL")
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.snapshots.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="draft")
    locale: Mapped[str] = mapped_column(sa.Text, nullable=False, default="en")

    # Skip-by-hash key: re-reason only when any of these differ.
    structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    critique_prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    thinking: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    # Phase 3B: hash of the rendered evidence block (part of the skip key), how
    # data values were exposed to the model, and how many column chunks.
    evidence_hash: Mapped[str | None] = mapped_column(sa.Text)
    exposure: Mapped[str | None] = mapped_column(sa.Text)
    chunks: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")

    business_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    purpose: Mapped[str] = mapped_column(sa.Text, nullable=False)
    grain: Mapped[str | None] = mapped_column(sa.Text)
    entity: Mapped[str | None] = mapped_column(sa.Text)
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    assumptions: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    unknowns: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    self_confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)

    # Aggregate of the table's column scores (see semantics.service).
    raw_score: Mapped[float | None] = mapped_column(sa.Float)
    calibrated_score: Mapped[float | None] = mapped_column(sa.Float)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    critique: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Phase 5B: an SME's decision (status sme_reviewed / sme_rejected); kept through
    # re-reasoning while the structure is unchanged (D16, D73)
    reviewed_by: Mapped[str | None] = mapped_column(sa.Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class ColumnSemantic(Base):
    """Versioned business meaning of a column, with its score breakdown."""

    __tablename__ = "column_semantic"
    __table_args__ = (
        sa.UniqueConstraint("column_id", "version", name="uq_column_semantic_column_version"),
        sa.Index(
            "uq_column_semantic_current",
            "column_id",
            unique=True,
            postgresql_where=sa.text("is_current"),
        ),
        sa.Index("ix_column_semantic_table_current", "table_id", "is_current"),
        sa.Index("ix_column_semantic_band", "band"),
        sa.Index("ix_column_semantic_search", "search_vector", postgresql_using="gin"),
        sa.Index(
            "ix_column_semantic_business_name_trgm",
            "business_name",
            postgresql_using="gin",
            postgresql_ops={"business_name": "gin_trgm_ops"},
        ),
        _status_check(),
        sa.CheckConstraint(
            "band IN (" + ", ".join(f"'{b}'" for b in SCORE_BANDS) + ")", name="band"
        ),
        sa.CheckConstraint(
            "coverage IN (" + ", ".join(f"'{c}'" for c in COVERAGE_LEVELS) + ")",
            name="coverage",
        ),
        {"schema": "semantics"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    table_semantic_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("semantics.table_semantic.id", ondelete="CASCADE"), nullable=False
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    column_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.columns.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("semantics.reasoning_run.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="draft")
    band: Mapped[str] = mapped_column(sa.Text, nullable=False)
    locale: Mapped[str] = mapped_column(sa.Text, nullable=False, default="en")

    structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)  # the column's
    table_structure_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)

    business_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    purpose: Mapped[str] = mapped_column(sa.Text, nullable=False)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(sa.Text)
    unit: Mapped[str | None] = mapped_column(sa.Text)
    value_domain: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    example_questions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    assumptions: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    unknowns: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    alternatives: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    self_confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    critique: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    raw_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    calibrated_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    weights_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    calibrator: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # Phase 3B: deterministic value type (from detectors when the data was
    # profiled), the model's own claim, the deepest evidence stage reached, and
    # whether the column looks unused (empty or constant).
    semantic_type: Mapped[str | None] = mapped_column(sa.Text)
    claimed_semantic_type: Mapped[str | None] = mapped_column(sa.Text)
    coverage: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="catalog")
    unused: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())

    # Phase 3 (PHASES decision 6): stored, never applied, in v1.
    pii_types: Mapped[list[str] | None] = mapped_column(ARRAY(sa.Text))
    pii_confidence: Mapped[float | None] = mapped_column(sa.Float)
    pii_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Phase 5B: an SME's decision (status sme_reviewed / sme_rejected); kept through
    # re-reasoning while the column's structure is unchanged (D16, D73)
    reviewed_by: Mapped[str | None] = mapped_column(sa.Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(sa.Text)
    # an SME's statement that the column holds personal data, or does not (5C, D74); None:
    # the detectors decide
    personal: Mapped[bool | None] = mapped_column(sa.Boolean)

    # Full-text search over the meaning (OUTLINE §8). 'english' matches the
    # English-only output decision; revisit with the locale work.
    search_vector: Mapped[Any | None] = mapped_column(
        TSVECTOR,
        sa.Computed(
            "to_tsvector('english'::regconfig, "
            "coalesce(business_name, '') || ' ' || coalesce(purpose, '') || ' ' || "
            "coalesce(entity, ''))",
            persisted=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class JoinHint(Base):
    """A join path: declared FK (confirmed), model suggestion (inferred) or SME.

    Status of a model suggestion: ``inferred`` (current), ``stale`` (the latest
    answer for its table no longer proposes it), ``refuted`` (a Stage 3 test of
    the claim found < 50 % of the rows in the target; migration 0006),
    ``rejected`` (by an SME).

    Names rather than catalog ids, because ``catalog.relations`` rows are
    replaced whenever a table's structure changes.
    """

    __tablename__ = "join_hint"
    __table_args__ = (
        sa.UniqueConstraint(
            "data_source_id",
            "from_schema",
            "from_table",
            "from_columns",
            "to_schema",
            "to_table",
            "to_columns",
            name="uq_join_hint_path",
        ),
        sa.Index("ix_join_hint_from", "data_source_id", "from_schema", "from_table"),
        sa.Index("ix_join_hint_to", "data_source_id", "to_schema", "to_table"),
        sa.CheckConstraint("source IN ('fk', 'llm', 'sme')", name="source"),
        sa.CheckConstraint(
            "status IN ('confirmed', 'inferred', 'rejected', 'refuted', 'stale')", name="status"
        ),
        {"schema": "semantics"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    from_schema: Mapped[str] = mapped_column(sa.Text, nullable=False)
    from_table: Mapped[str] = mapped_column(sa.Text, nullable=False)
    from_columns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    to_schema: Mapped[str] = mapped_column(sa.Text, nullable=False)
    to_table: Mapped[str] = mapped_column(sa.Text, nullable=False)
    to_columns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    relation_name: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    rationale: Mapped[str | None] = mapped_column(sa.Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("semantics.reasoning_run.id", ondelete="SET NULL")
    )
    prompt_version: Mapped[str | None] = mapped_column(sa.Text)
    model: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class GlossaryTerm(Base):
    """Business terminology for the human-in-the-loop step.

    Filled by SMEs (Phase 5B review screens) or imported from an existing company
    glossary, which is then treated as ground truth. ``data_source_id`` NULL means the
    term applies to every data source. A withdrawn term is kept with status
    ``rejected`` and no longer reaches the SQL generator.
    """

    __tablename__ = "glossary_term"
    __table_args__ = (
        sa.Index(
            "uq_glossary_term_scope_term_locale",
            sa.text("coalesce(data_source_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            sa.text("lower(term)"),
            "locale",
            unique=True,
        ),
        sa.Index(
            "ix_glossary_term_term_trgm",
            "term",
            postgresql_using="gin",
            postgresql_ops={"term": "gin_trgm_ops"},
        ),
        sa.CheckConstraint("source IN ('sme', 'import')", name="source"),
        sa.CheckConstraint("status IN ('proposed', 'approved', 'rejected')", name="status"),
        {"schema": "semantics"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE")
    )
    term: Mapped[str] = mapped_column(sa.Text, nullable=False)
    definition: Mapped[str] = mapped_column(sa.Text, nullable=False)
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    locale: Mapped[str] = mapped_column(sa.Text, nullable=False, default="en")
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="approved")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class Embedding(Base):
    """Vector for one table or column semantic version.

    Attached to the semantic *version*, so a re-reasoned meaning gets a new
    vector while the old one stays with the old version. ``model`` and
    ``dimensions`` are recorded so a model swap is an explicit re-embed.
    """

    __tablename__ = "embedding"
    __table_args__ = (
        sa.CheckConstraint(
            "(subject_kind = 'table' AND table_semantic_id IS NOT NULL "
            "AND column_semantic_id IS NULL) OR "
            "(subject_kind = 'column' AND column_semantic_id IS NOT NULL "
            "AND table_semantic_id IS NULL)",
            name="subject",
        ),
        sa.Index(
            "uq_embedding_table_semantic_model",
            "table_semantic_id",
            "model",
            unique=True,
            postgresql_where=sa.text("table_semantic_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_embedding_column_semantic_model",
            "column_semantic_id",
            "model",
            unique=True,
            postgresql_where=sa.text("column_semantic_id IS NOT NULL"),
        ),
        sa.Index(
            "ix_embedding_vector_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        {"schema": "semantics"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    subject_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)  # table | column
    table_semantic_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("semantics.table_semantic.id", ondelete="CASCADE")
    )
    column_semantic_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("semantics.column_semantic.id", ondelete="CASCADE")
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    column_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.columns.id", ondelete="CASCADE")
    )
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    dimensions: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class EvidenceItem(Base):
    """One entry of a column version's evidence ledger (Phase 3B, heuristic-v2).

    Stored per scored version, so a score can be traced to its evidence and
    Phase 6 can fit a calibrator on SME-reviewed examples.
    """

    __tablename__ = "evidence_item"
    __table_args__ = (
        sa.Index("ix_evidence_item_column_semantic", "column_semantic_id"),
        sa.Index("ix_evidence_item_kind", "kind", "direction"),
        sa.CheckConstraint("direction IN ('supports', 'contradicts')", name="direction"),
        sa.CheckConstraint(
            "source IN ('catalog', 'module', 'profile', 'detector', 'identity', 'test', 'sme')",
            name="source",
        ),
        {"schema": "semantics"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    column_semantic_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("semantics.column_semantic.id", ondelete="CASCADE"), nullable=False
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    direction: Mapped[str] = mapped_column(sa.Text, nullable=False)
    strength: Mapped[float] = mapped_column(sa.Float, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    ref: Mapped[str | None] = mapped_column(sa.Text)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class ReviewItem(Base):
    """The SME review queue (Phase 3B Stage 4; the UI is Phase 5).

    One *open* item per column at most; a newer scored version supersedes it.
    ``priority = (1 - score) x importance``; importance comes from row count,
    foreign-key degree and whether the column is a key or indexed.
    """

    __tablename__ = "review_item"
    __table_args__ = (
        sa.Index(
            "uq_review_item_open_column",
            "column_id",
            unique=True,
            postgresql_where=sa.text("status = 'open'"),
        ),
        sa.Index("ix_review_item_queue", "data_source_id", "status", "priority"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in REVIEW_STATUSES) + ")", name="status"
        ),
        sa.CheckConstraint("subject_kind IN ('column', 'table')", name="subject_kind"),
        {"schema": "semantics"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False
    )
    column_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.columns.id", ondelete="CASCADE")
    )
    column_semantic_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("semantics.column_semantic.id", ondelete="SET NULL")
    )
    subject_kind: Mapped[str] = mapped_column(sa.Text, nullable=False, default="column")
    reasons: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    priority: Mapped[float] = mapped_column(sa.Float, nullable=False)
    score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    importance: Mapped[float] = mapped_column(sa.Float, nullable=False)
    coverage: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="open")
    assignee: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
