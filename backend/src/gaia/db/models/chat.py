"""Chat and query models (Phase 4).

``query_run`` (4A) records every statement that went through the SQL guard —
accepted or rejected — and what happened when it ran: the statement as
submitted and as executed (rendered from the validated AST), the guard's
findings, the status, rows, the time, and whether the row cap was reached.
Each run **pins** the catalog snapshot it was validated against and the
semantic versions of the objects it read (PHASES Phase 4 acceptance), so an
answer can always be traced to the metadata it relied on.

Repairs (4C) are runs of their own, linked by ``parent_id`` and numbered by
``attempt``; the model columns stay empty until generation exists.
Conversations and messages come with the API (4E), in their own migration.

``clarification`` (D61, migration 0008) keeps every question Gaia asked back: the
options, the one Gaia would have chosen (its best guess, with the SQL the guard
checked but nobody ran), and — once the user answers — what the user chose,
whether that was Gaia's choice, and the run that answered. It is the record a
later mechanism needs before Gaia may answer such a question without asking, and
the measurement that decides whether it may: how often Gaia's choice is the
user's. ``origin`` keeps golden and test runs out of that measurement.

``conversation`` and ``message`` (Phase 4E, migration 0009) hold the chat: the user's
turns and Gaia's — an answer (narrative, result, interpretation, assumptions,
confidence), a clarification, "cannot answer", or a failure — in order. An answer
keeps at most ``chat.keep_rows`` rows, with personal-data columns masked in the
stored copy: the table is shown again later without keeping a full copy of the
customer's data, nor any personal data, at rest.
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

__all__ = ["CLARIFICATION_STATUSES", "MESSAGE_KINDS", "QUERY_RUN_ORIGINS", "QUERY_RUN_STATUSES",
           "Clarification", "Conversation", "Message", "QueryRun"]

MESSAGE_KINDS = ("question", "clarification_answer", "answer", "clarification",
                 "cannot_answer", "failed")

CLARIFICATION_STATUSES = ("open", "answered", "abandoned")

QUERY_RUN_STATUSES = ("rejected", "ok", "timeout", "lock_timeout", "error")
QUERY_RUN_ORIGINS = ("cli", "api", "golden", "test")


class QueryRun(Base):
    __tablename__ = "query_run"
    __table_args__ = (
        sa.Index("ix_query_run_source_created", "data_source_id", "created_at"),
        sa.Index("ix_query_run_parent", "parent_id"),
        sa.CheckConstraint(
            "status IN ('rejected', 'ok', 'timeout', 'lock_timeout', 'error')", name="status"
        ),
        sa.CheckConstraint("origin IN ('cli', 'api', 'golden', 'test')", name="origin"),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.snapshots.id", ondelete="CASCADE"), nullable=False
    )
    # {"schema.object": {"id": table_semantic id, "version": n} | null} for every object read
    semantic_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("chat.query_run.id", ondelete="SET NULL")
    )
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False)
    question: Mapped[str | None] = mapped_column(sa.Text)
    dialect: Mapped[str] = mapped_column(sa.Text, nullable=False)
    submitted_sql: Mapped[str] = mapped_column(sa.Text, nullable=False)
    executed_sql: Mapped[str | None] = mapped_column(sa.Text)
    # errors, warnings, tables, joins, row_limit, limit_injected (GuardResult.as_dict)
    guard: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    row_count: Mapped[int | None] = mapped_column(sa.Integer)
    row_cap_reached: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    elapsed_ms: Mapped[int | None] = mapped_column(sa.Integer)
    error: Mapped[str | None] = mapped_column(sa.Text)
    model: Mapped[str | None] = mapped_column(sa.Text)
    prompt_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    completion_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    llm_ms: Mapped[int | None] = mapped_column(sa.Integer)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class Clarification(Base):
    __tablename__ = "clarification"
    __table_args__ = (
        sa.Index("ix_clarification_source_created", "data_source_id", "created_at"),
        sa.Index("ix_clarification_status", "status"),
        sa.CheckConstraint("status IN ('open', 'answered', 'abandoned')", name="status"),
        sa.CheckConstraint("origin IN ('cli', 'api', 'golden', 'test')", name="origin"),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.snapshots.id", ondelete="SET NULL")
    )
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False)
    asked_by: Mapped[str | None] = mapped_column(sa.Text)  # a user, once identities exist (P5-1)
    question: Mapped[str] = mapped_column(sa.Text, nullable=False)
    clarification_question: Mapped[str] = mapped_column(sa.Text, nullable=False)
    options: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)  # as shown
    # the model's index of each shown option (migration 0011); NULL: shown as written
    option_order: Mapped[list[int] | None] = mapped_column(JSONB)
    ambiguity: Mapped[str | None] = mapped_column(sa.Text)
    # Gaia's best guess: the option it would choose (its position as shown), and that
    # option's answer. Never sent to a chat client (D61, 5.0 T5).
    default_option: Mapped[int | None] = mapped_column(sa.Integer)
    best_guess_interpretation: Mapped[str | None] = mapped_column(sa.Text)
    best_guess_assumptions: Mapped[list[str]] = mapped_column(JSONB, nullable=False,
                                                              default=list)
    best_guess_sql: Mapped[str | None] = mapped_column(sa.Text)
    best_guess_valid: Mapped[bool | None] = mapped_column(sa.Boolean)  # the guard's verdict
    model: Mapped[str | None] = mapped_column(sa.Text)
    # the user's turn
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="open")
    user_answer: Mapped[str | None] = mapped_column(sa.Text)
    chosen_option: Mapped[int | None] = mapped_column(sa.Integer)  # None: the user's own words
    took_default: Mapped[bool | None] = mapped_column(sa.Boolean)
    answer_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("chat.query_run.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    answered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Conversation(Base):
    __tablename__ = "conversation"
    __table_args__ = (
        sa.Index("ix_conversation_source_updated", "data_source_id", "updated_at"),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[str | None] = mapped_column(sa.Text)  # a user, once identities exist
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (
        sa.UniqueConstraint("conversation_id", "seq", name="uq_message_conversation_seq"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="role"),
        sa.CheckConstraint(
            "kind IN ('question', 'clarification_answer', 'answer', 'clarification', "
            "'cannot_answer', 'failed')", name="kind"),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("chat.conversation.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    query_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("chat.query_run.id", ondelete="SET NULL")
    )
    clarification_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("chat.clarification.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

