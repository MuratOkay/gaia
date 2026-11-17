"""Need-to-know (Phase 5C, D74) and the chat's audit trail (5D).

* ``auth.category`` — data categories; ``auth.object_category`` — each object's category
  (proposed by Gaia, confirmed by an SME or set by an administrator; one current row).
* ``auth.access_grant`` — privilege ``category`` with ``category_id``.
* ``semantics.column_semantic.personal`` — an SME's statement that a column holds personal
  data (or does not).
* ``chat.message.kind`` gains ``refused``; ``chat.query_run.asked_by`` keeps who asked.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
OLD_PRIVILEGES = "privilege IN ('user', 'sme', 'admin', 'auditor', 'pii_processor')"
NEW_PRIVILEGES = "privilege IN ('user', 'sme', 'admin', 'auditor', 'pii_processor', 'category')"
OLD_KINDS = ("kind IN ('question', 'clarification_answer', 'answer', 'clarification', "
             "'cannot_answer', 'failed')")
NEW_KINDS = ("kind IN ('question', 'clarification_answer', 'answer', 'clarification', "
             "'cannot_answer', 'failed', 'refused')")


def upgrade() -> None:
    op.create_table(
        "category",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("keywords", ARRAY(sa.Text()), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("retired_by", sa.Text(), nullable=True),
        sa.Column("retired_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_category")),
        schema="auth",
    )
    op.create_index("uq_category_name", "category", [sa.text("lower(name)")], unique=True,
                    schema="auth")
    op.create_table(
        "object_category",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.CheckConstraint("status IN ('proposed', 'confirmed')",
                           name=op.f("ck_object_category_status")),
        sa.CheckConstraint("source IN ('gaia', 'sme', 'admin')",
                           name=op.f("ck_object_category_source")),
        sa.ForeignKeyConstraint(["category_id"], ["auth.category.id"], ondelete="CASCADE",
                                name=op.f("fk_object_category_category_id_category")),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE",
                                name=op.f("fk_object_category_data_source_id_data_sources")),
        sa.ForeignKeyConstraint(["table_id"], ["catalog.tables.id"], ondelete="CASCADE",
                                name=op.f("fk_object_category_table_id_tables")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_object_category")),
        schema="auth",
    )
    op.create_index("uq_object_category_current", "object_category", ["table_id"],
                    unique=True, schema="auth", postgresql_where=sa.text("is_current"))
    op.create_index("ix_object_category_source", "object_category",
                    ["data_source_id", "is_current"], schema="auth")

    op.add_column("access_grant", sa.Column("category_id", sa.Uuid(), nullable=True),
                  schema="auth")
    op.create_foreign_key(op.f("fk_access_grant_category_id_category"), "access_grant",
                          "category", ["category_id"], ["id"], source_schema="auth",
                          referent_schema="auth", ondelete="CASCADE")
    op.drop_constraint(op.f("ck_access_grant_privilege"), "access_grant", schema="auth")
    op.create_check_constraint(op.f("ck_access_grant_privilege"), "access_grant",
                               NEW_PRIVILEGES, schema="auth")
    op.create_check_constraint(op.f("ck_access_grant_category_grant"), "access_grant",
                               "(privilege = 'category') = (category_id IS NOT NULL)",
                               schema="auth")

    op.add_column("column_semantic", sa.Column("personal", sa.Boolean(), nullable=True),
                  schema="semantics")
    op.add_column("query_run", sa.Column("asked_by", sa.Text(), nullable=True), schema="chat")
    op.drop_constraint(op.f("ck_message_kind"), "message", schema="chat")
    op.create_check_constraint(op.f("ck_message_kind"), "message", NEW_KINDS, schema="chat")


def downgrade() -> None:
    op.execute("DELETE FROM chat.message WHERE kind = 'refused'")
    op.drop_constraint(op.f("ck_message_kind"), "message", schema="chat")
    op.create_check_constraint(op.f("ck_message_kind"), "message", OLD_KINDS, schema="chat")
    op.drop_column("query_run", "asked_by", schema="chat")
    op.drop_column("column_semantic", "personal", schema="semantics")
    op.execute("DELETE FROM auth.access_grant WHERE privilege = 'category'")
    op.drop_constraint(op.f("ck_access_grant_category_grant"), "access_grant", schema="auth")
    op.drop_constraint(op.f("ck_access_grant_privilege"), "access_grant", schema="auth")
    op.create_check_constraint(op.f("ck_access_grant_privilege"), "access_grant",
                               OLD_PRIVILEGES, schema="auth")
    op.drop_constraint(op.f("fk_access_grant_category_id_category"), "access_grant",
                       schema="auth", type_="foreignkey")
    op.drop_column("access_grant", "category_id", schema="auth")
    op.drop_index("ix_object_category_source", table_name="object_category", schema="auth")
    op.drop_index("uq_object_category_current", table_name="object_category", schema="auth")
    op.drop_table("object_category", schema="auth")
    op.drop_index("uq_category_name", table_name="category", schema="auth")
    op.drop_table("category", schema="auth")
