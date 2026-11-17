"""Identity and access (Phase 5A, D68–D70): schema ``auth`` and ``audit.auth_event``.

``auth.directory`` (company directories, service-account password encrypted),
``auth.account`` (directory accounts and the one local account), ``auth.directory_role``
and ``auth.account_role`` (roles read from the directory), ``auth.access_grant`` (Gaia roles
and permissions, never deleted), ``auth.session`` (server-side sessions, hashes only), and
the hash-chained ``audit.auth_event``.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

A = "auth"
J = postgresql.JSONB(astext_type=sa.Text())
NOW = sa.text("now()")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS auth")
    op.execute("COMMENT ON SCHEMA auth IS 'Directories, accounts, roles, grants, sessions'")
    op.create_table(
        "directory",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("settings", J, nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=True),
        sa.Column("secret_key_id", sa.Text(), nullable=True),
        sa.Column("probed_at", TS, nullable=True),
        sa.Column("probe", J, nullable=True),
        sa.Column("synced_at", TS, nullable=True),
        sa.Column("sync", J, nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.CheckConstraint("kind IN ('ldap', 'entra')", name=op.f("ck_directory_kind")),
        sa.CheckConstraint("status IN ('unverified', 'verified', 'failed', 'disabled')",
                           name=op.f("ck_directory_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_directory")),
        sa.UniqueConstraint("name", name="uq_directory_name"),
        schema=A,
    )
    op.create_table(
        "account",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("directory_id", sa.Uuid(), nullable=True),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_local", sa.Boolean(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("password_set_at", TS, nullable=True),
        sa.Column("setup_code_hash", sa.Text(), nullable=True),
        sa.Column("setup_code_expires_at", TS, nullable=True),
        sa.Column("failed_sign_ins", sa.Integer(), nullable=False),
        sa.Column("locked_until", TS, nullable=True),
        sa.Column("last_sign_in_at", TS, nullable=True),
        sa.Column("synced_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.CheckConstraint("is_local = (directory_id IS NULL)",
                           name=op.f("ck_account_local_has_no_directory")),
        sa.CheckConstraint("is_local OR password_hash IS NULL",
                           name=op.f("ck_account_no_directory_password")),
        sa.ForeignKeyConstraint(["directory_id"], ["auth.directory.id"], ondelete="CASCADE",
                                name=op.f("fk_account_directory_id_directory")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account")),
        schema=A,
    )
    op.create_index("uq_account_directory_login", "account", ["directory_id", "login"],
                    unique=True, schema=A, postgresql_where=sa.text("directory_id IS NOT NULL"))
    op.create_index("uq_account_one_local", "account", ["is_local"], unique=True, schema=A,
                    postgresql_where=sa.text("is_local"))
    op.create_table(
        "directory_role",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("directory_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("members", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", TS, server_default=NOW, nullable=False),
        sa.Column("last_seen_at", TS, server_default=NOW, nullable=False),
        sa.Column("gone_at", TS, nullable=True),
        sa.CheckConstraint("source IN ('group', 'attribute', 'ou')",
                           name=op.f("ck_directory_role_source")),
        sa.ForeignKeyConstraint(["directory_id"], ["auth.directory.id"], ondelete="CASCADE",
                                name=op.f("fk_directory_role_directory_id_directory")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_directory_role")),
        sa.UniqueConstraint("directory_id", "source", "ref", name="uq_directory_role_ref"),
        schema=A,
    )
    op.create_table(
        "account_role",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("directory_role_id", sa.Uuid(), nullable=False),
        sa.Column("seen_at", TS, server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["auth.account.id"], ondelete="CASCADE",
                                name=op.f("fk_account_role_account_id_account")),
        sa.ForeignKeyConstraint(["directory_role_id"], ["auth.directory_role.id"],
                                ondelete="CASCADE",
                                name=op.f("fk_account_role_directory_role_id_directory_role")),
        sa.PrimaryKeyConstraint("account_id", "directory_role_id", name=op.f("pk_account_role")),
        schema=A,
    )
    op.create_table(
        "access_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("privilege", sa.Text(), nullable=False),
        sa.Column("directory_role_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("data_source_id", sa.Uuid(), nullable=True),
        sa.Column("granted_by", sa.Text(), nullable=False),
        sa.Column("granted_at", TS, server_default=NOW, nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.CheckConstraint("privilege IN ('user', 'sme', 'admin', 'auditor', 'pii_processor')",
                           name=op.f("ck_access_grant_privilege")),
        sa.CheckConstraint("(directory_role_id IS NULL) <> (account_id IS NULL)",
                           name=op.f("ck_access_grant_one_subject")),
        sa.CheckConstraint("(privilege = 'sme') = (data_source_id IS NOT NULL)",
                           name=op.f("ck_access_grant_sme_per_data_source")),
        sa.ForeignKeyConstraint(["account_id"], ["auth.account.id"], ondelete="CASCADE",
                                name=op.f("fk_access_grant_account_id_account")),
        sa.ForeignKeyConstraint(["data_source_id"], ["catalog.data_sources.id"],
                                ondelete="CASCADE",
                                name=op.f("fk_access_grant_data_source_id_data_sources")),
        sa.ForeignKeyConstraint(["directory_role_id"], ["auth.directory_role.id"],
                                ondelete="CASCADE",
                                name=op.f("fk_access_grant_directory_role_id_directory_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_grant")),
        schema=A,
    )
    op.create_index("ix_grant_current_role", "access_grant", ["directory_role_id"], schema=A,
                    postgresql_where=sa.text("revoked_at IS NULL"))
    op.create_index("ix_grant_current_account", "access_grant", ["account_id"], schema=A,
                    postgresql_where=sa.text("revoked_at IS NULL"))
    op.create_table(
        "session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_hash", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("privileges", J, nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("last_seen_at", TS, server_default=NOW, nullable=False),
        sa.Column("rechecked_at", TS, server_default=NOW, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["auth.account.id"], ondelete="CASCADE",
                                name=op.f("fk_session_account_id_account")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session")),
        sa.UniqueConstraint("token_hash", name="uq_session_token"),
        schema=A,
    )
    op.create_index("ix_session_account", "session", ["account_id"], schema=A)
    op.create_table(
        "auth_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("detail", J, nullable=False),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_event")),
        sa.UniqueConstraint("seq", name="uq_auth_event_seq"),
        schema="audit",
    )
    op.create_index("ix_auth_event_at", "auth_event", ["at"], schema="audit")
    op.create_index("ix_auth_event_action", "auth_event", ["action"], schema="audit")


def downgrade() -> None:
    op.drop_table("auth_event", schema="audit")
    for table in ("session", "access_grant", "account_role", "directory_role", "account",
                  "directory"):
        op.drop_table(table, schema=A)
    op.execute("DROP SCHEMA IF EXISTS auth")
