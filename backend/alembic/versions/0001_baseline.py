"""Baseline: required extensions and Gaia schemas.

Idempotent by design — ``infra/postgres/init/001_extensions.sql`` also runs on a
fresh Docker volume, but migrations must be sufficient on their own for managed
PostgreSQL instances where init scripts are not available.

Revision ID: 0001
Revises:
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ("vector", "pg_trgm", "unaccent", "btree_gin", "pgcrypto")

SCHEMAS = (
    ("catalog", "Raw and normalized target-database metadata snapshots"),
    ("profiling", "Table and column profile measurements"),
    ("semantics", "Reasoned semantic models, scores, embeddings"),
    ("chat", "Conversations, messages, generated query runs"),
    ("evaluation", "Golden questions and evaluation results"),
    ("audit", "LLM call log and detected schema changes"),
)


def upgrade() -> None:
    for extension in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")

    for name, comment in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {name}")
        op.execute(f"COMMENT ON SCHEMA {name} IS '{comment}'")


def downgrade() -> None:
    # Extensions are intentionally left in place: dropping pgvector would
    # destroy embedding columns in other schemas.
    for name, _ in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
