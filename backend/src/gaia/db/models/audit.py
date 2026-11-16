"""Audit records: every LLM call, successful or not (OUTLINE §8 ``audit``).

Raw payloads are kept so failures are recorded, not swallowed (PHASES,
cross-cutting "Failure"), and so the cost/latency report can be rebuilt from
the database at any time. Secrets never reach this table: requests are stored
as a prompt hash plus prompt version, never with headers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gaia.db.models.base import Base
from gaia.db.models.catalog import new_uuid


class LLMCall(Base):
    __tablename__ = "llm_call"
    __table_args__ = (
        sa.Index("ix_llm_call_run", "run_id"),
        sa.Index("ix_llm_call_created", "created_at"),
        sa.Index("ix_llm_call_prompt_hash", "prompt_hash"),
        {"schema": "audit"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("semantics.reasoning_run.id", ondelete="SET NULL")
    )
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="SET NULL")
    )
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="SET NULL")
    )
    purpose: Mapped[str] = mapped_column(sa.Text, nullable=False)  # semantics | critique
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    thinking: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    json_mode: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    parsed_ok: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(sa.Text)
    finish_reason: Mapped[str | None] = mapped_column(sa.Text)
    request_id: Mapped[str | None] = mapped_column(sa.Text)
    prompt_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    transport_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    # Cost is free on the gateway until 2026-12-01; kept nullable for later.
    cost: Mapped[float | None] = mapped_column(sa.Numeric(12, 6))
    raw_response: Mapped[str | None] = mapped_column(sa.Text)
    reasoning: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class ConnectionChange(Base):
    """Who added, changed, probed, disabled or removed a data source, and when (Phase 4F,
    OUTLINE §24). ``detail`` names what changed and the probe's verdicts — never a value
    of a credential."""

    __tablename__ = "connection_change"
    __table_args__ = (
        sa.Index("ix_connection_change_source", "data_source_id", "created_at"),
        sa.CheckConstraint(
            "action IN ('created', 'updated', 'probed', 'disabled', 'enabled', 'removed', "
            "'rotated')", name="action"),
        {"schema": "audit"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="SET NULL")
    )
    data_source_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(sa.Text)  # a user, once identities exist (P5-1)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

