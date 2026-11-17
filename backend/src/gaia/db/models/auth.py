"""Identity and access (Phase 5A, D68): directories, accounts, directory roles, grants,
sessions. Schema ``auth`` (migration 0012).

* ``directory`` — a company directory an administrator connected (AD/LDAP; Entra ID in
  5A.2): non-secret settings in JSONB, the service account's password encrypted like a
  data source's (D65), and the last probe, as for data sources.
* ``account`` — a person who signed in (or was read by a directory sync), or the **one**
  local administrator (``is_local``; a partial unique index allows a single such row).
  Directory accounts never store a password: the directory checks it at every sign-in.
* ``directory_role`` — what the administrator chose to read as roles: groups, values of a
  user attribute, or OUs (``source``); ``account_role`` — who had which, when last read.
* ``access_grant`` — who holds a Gaia role or permission: ``user``, ``sme`` (per data source),
  ``admin``, ``auditor``, ``pii_processor``; granted to a directory role or to one
  account; never deleted — a revocation is dated and signed, for the auditor (D70).
* ``session`` — server-side sessions (P5-8): only a hash of the token and of its CSRF
  token is stored; the privileges in force are kept with it and re-read from the
  directory every ``auth.recheck_minutes``.

Need-to-know (Phase 5C, D74; migration 0015):

* ``category`` — a data category (a business domain: Sales, Finance, HR …) the
  administrator defines; ``keywords`` help Gaia propose it for objects.
* ``object_category`` — what an object's data *is*: one category per table or view,
  proposed by Gaia, confirmed by an SME (or set by an administrator); the current row
  per object is the one in force. Keyed by the catalog's table id, so re-reasoning never
  touches it.
* ``access_grant`` with privilege ``category`` — a directory role or an account may read
  the objects of that category. Nothing is readable until granted (deny by default).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gaia.db.models.base import Base
from gaia.db.models.catalog import new_uuid

S = "auth"
APP_ROLES = ("user", "sme", "admin", "auditor")
PRIVILEGES = (*APP_ROLES, "pii_processor", "category")
OBJECT_CATEGORY_STATUSES = ("proposed", "confirmed")
OBJECT_CATEGORY_SOURCES = ("gaia", "sme", "admin")
PROPOSAL_BASES = ("direct", "view", "related", "weak")  # D77
DIRECTORY_KINDS = ("ldap", "entra")
DIRECTORY_STATUSES = ("unverified", "verified", "failed", "disabled")
ROLE_SOURCES = ("group", "attribute", "ou")


def _in(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({', '.join(repr(v) for v in values)})", name=name)


def _now() -> Any:
    return sa.func.now()


class Directory(Base):
    __tablename__ = "directory"
    __table_args__ = (
        sa.UniqueConstraint("name", name="uq_directory_name"),
        _in("kind", DIRECTORY_KINDS, "kind"),
        _in("status", DIRECTORY_STATUSES, "status"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="unverified")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    secret: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    secret_key_id: Mapped[str | None] = mapped_column(sa.Text)
    probed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    probe: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    synced_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    sync: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                 server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                 server_default=_now(), onupdate=_now())


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (
        sa.Index("uq_account_directory_login", "directory_id", "login", unique=True,
                 postgresql_where=sa.text("directory_id IS NOT NULL")),
        # exactly one local account can exist (D68)
        sa.Index("uq_account_one_local", "is_local", unique=True,
                 postgresql_where=sa.text("is_local")),
        sa.CheckConstraint("is_local = (directory_id IS NULL)", name="local_has_no_directory"),
        sa.CheckConstraint("is_local OR password_hash IS NULL", name="no_directory_password"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    directory_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(f"{S}.directory.id", ondelete="CASCADE"))
    login: Mapped[str] = mapped_column(sa.Text, nullable=False)  # lower case
    external_id: Mapped[str | None] = mapped_column(sa.Text)  # DN / objectGUID / Entra oid
    display_name: Mapped[str | None] = mapped_column(sa.Text)
    email: Mapped[str | None] = mapped_column(sa.Text)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    is_local: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    password_hash: Mapped[str | None] = mapped_column(sa.Text)  # the local account only
    password_set_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    setup_code_hash: Mapped[str | None] = mapped_column(sa.Text)
    setup_code_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    failed_sign_ins: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_sign_in_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                 server_default=_now())
    # D78: the directory's object behind this login is not the one this account belongs to —
    # the id it has now, and since when; sign-ins are refused until an administrator re-links
    # the account or starts it anew (migration 0017)
    pending_external_id: Mapped[str | None] = mapped_column(sa.Text)
    identity_changed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class DirectoryRole(Base):
    __tablename__ = "directory_role"
    __table_args__ = (
        sa.UniqueConstraint("directory_id", "source", "ref", name="uq_directory_role_ref"),
        _in("source", ROLE_SOURCES, "source"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    directory_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey(f"{S}.directory.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    ref: Mapped[str] = mapped_column(sa.Text, nullable=False)  # group DN, value, OU DN (lower)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    members: Mapped[int | None] = mapped_column(sa.Integer)  # at the last sync
    first_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                    server_default=_now())
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                   server_default=_now())
    gone_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class AccountRole(Base):
    __tablename__ = "account_role"
    __table_args__ = ({"schema": S},)

    account_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey(f"{S}.account.id", ondelete="CASCADE"), primary_key=True)
    directory_role_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey(f"{S}.directory_role.id", ondelete="CASCADE"), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                              server_default=_now())


class Category(Base):
    __tablename__ = "category"
    __table_args__ = (
        sa.Index("uq_category_name", sa.text("lower(name)"), unique=True),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                 server_default=_now())
    retired_by: Mapped[str | None] = mapped_column(sa.Text)
    retired_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ObjectCategory(Base):
    __tablename__ = "object_category"
    __table_args__ = (
        sa.Index("uq_object_category_current", "table_id", unique=True,
                 postgresql_where=sa.text("is_current")),
        sa.Index("ix_object_category_source", "data_source_id", "is_current"),
        _in("status", OBJECT_CATEGORY_STATUSES, "status"),
        _in("source", OBJECT_CATEGORY_SOURCES, "source"),
        sa.CheckConstraint("basis IS NULL OR basis IN ('direct', 'view', 'related', 'weak')",
                           name="basis"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"), nullable=False)
    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalog.tables.id", ondelete="CASCADE"), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey(f"{S}.category.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.Text)
    decided_by: Mapped[str | None] = mapped_column(sa.Text)
    decided_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                 server_default=_now())
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    # what Gaia's proposal rests on (D77, migration 0016): direct | view | related | weak
    basis: Mapped[str | None] = mapped_column(sa.Text)


class Grant(Base):
    __tablename__ = "access_grant"
    __table_args__ = (
        _in("privilege", PRIVILEGES, "privilege"),
        sa.CheckConstraint("(directory_role_id IS NULL) <> (account_id IS NULL)",
                           name="one_subject"),
        sa.CheckConstraint("(privilege = 'sme') = (data_source_id IS NOT NULL)",
                           name="sme_per_data_source"),
        sa.CheckConstraint("(privilege = 'category') = (category_id IS NOT NULL)",
                           name="category_grant"),
        sa.Index("ix_grant_current_role", "directory_role_id",
                 postgresql_where=sa.text("revoked_at IS NULL")),
        sa.Index("ix_grant_current_account", "account_id",
                 postgresql_where=sa.text("revoked_at IS NULL")),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    privilege: Mapped[str] = mapped_column(sa.Text, nullable=False)
    directory_role_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(f"{S}.directory_role.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(f"{S}.account.id", ondelete="CASCADE"))
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("catalog.data_sources.id", ondelete="CASCADE"))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(f"{S}.category.id", ondelete="CASCADE"))
    granted_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                 server_default=_now())
    note: Mapped[str | None] = mapped_column(sa.Text)
    revoked_by: Mapped[str | None] = mapped_column(sa.Text)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Session(Base):
    __tablename__ = "session"
    __table_args__ = (
        sa.UniqueConstraint("token_hash", name="uq_session_token"),
        sa.Index("ix_session_account", "account_id"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    token_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey(f"{S}.account.id", ondelete="CASCADE"), nullable=False)
    privileges: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                 server_default=_now())
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                   server_default=_now())
    rechecked_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                   server_default=_now())
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(sa.Text)
    ip: Mapped[str | None] = mapped_column(sa.Text)
    user_agent: Mapped[str | None] = mapped_column(sa.Text)


class PendingLogin(Base):
    """A sign-in started with an OpenID Connect provider (Entra ID, 5A.2) and not finished
    yet: single use, ``auth.oidc_minutes`` long. ``state`` and the browser binding are kept
    as hashes; the PKCE verifier encrypted with the master key (D65), bound to this row."""

    __tablename__ = "pending_login"
    __table_args__ = (
        sa.UniqueConstraint("state_hash", name="uq_pending_login_state"),
        sa.Index("ix_pending_login_expires", "expires_at"),
        {"schema": S},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    directory_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey(f"{S}.directory.id", ondelete="CASCADE"), nullable=False)
    state_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    binding_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    nonce: Mapped[str] = mapped_column(sa.Text, nullable=False)
    verifier: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    verifier_key_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    ip: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False,
                                                 server_default=_now())
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
