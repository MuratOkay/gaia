"""Sign-in, sessions, the local account, directories and grants (Phase 5A; D68–D70).

Every decision that changes who may do what is recorded in the hash-chained audit
(``gaia.auth.audit``) in the same transaction as the change.

**Sign-in.** ``gaia-admin`` with no directory named is the local account (argon2id,
D68); anything else goes to a directory. The answer to a refused sign-in is always the
same ("sign-in failed") whatever the reason — wrong password, unknown login, locked,
disabled, or no Gaia role — and the audit keeps the real reason. After
``auth.max_failed_sign_ins`` failures an account is locked for ``auth.lockout_minutes``
before the directory is even asked (the directory's own lockout policy stays the outer
limit).

**Sessions** (P5-8). The cookie carries a random token; the database keeps its hash, the
hash of the CSRF token, the privileges in force, an absolute expiry and the last activity
(idle expiry). Every ``auth.recheck_minutes`` the directory is asked again (service
account only): an account disabled or deleted there, or left with no Gaia role, is
signed out. If the directory cannot be reached, the session lives on with its last
privileges for at most one more interval (retried once a minute), then it ends — fail
closed, but not at the first network hiccup.
"""

from __future__ import annotations

import asyncio
import getpass
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.auth import audit
from gaia.auth.ldap import DirectoryError, DirectoryUser, LdapDirectory, LdapSettings, RoleRef
from gaia.auth.passwords import (
    PasswordPolicyError,
    check_policy,
    hash_password,
    needs_rehash,
    new_token,
    same_token,
    token_hash,
    verify_password,
)
from gaia.auth.privileges import Effective, Held, effective
from gaia.core.config import Settings
from gaia.datasources.crypto import decrypt, encrypt
from gaia.db.models.auth import (
    APP_ROLES,
    PRIVILEGES,
    Account,
    AccountRole,
    Directory,
    DirectoryRole,
    Grant,
    PendingLogin,
)
from gaia.db.models.auth import Session as SessionRow
from gaia.db.models.catalog import DataSource

__all__ = ["LOCAL_LOGIN", "AuthError", "AuthService", "NotSignedIn", "Principal",
           "SignInRefused", "cli_actor"]

LOCAL_LOGIN = "gaia-admin"
REFUSED = "sign-in failed"
_SECRET_NS = uuid.UUID("0b7c3c4e-5a1d-4d4f-9a52-6a1f0d7e2c11")  # directory secrets' AAD
RETRY_S = 60


class AuthError(RuntimeError):
    """A request the caller can correct (a bad name, a missing setting, a policy)."""


class SignInRefused(AuthError):  # noqa: N818 - the public message is deliberately vague
    """The one public answer to a refused sign-in; ``reason`` is for the audit only."""

    def __init__(self, reason: str, actor: str | None = None) -> None:
        super().__init__(REFUSED)
        self.reason = reason
        self.actor = actor  # who, when the directory had already identified them


class NotSignedIn(AuthError):  # noqa: N818
    """No session, an expired or revoked one, or a CSRF token that does not match."""


@dataclass
class Principal:
    account_id: uuid.UUID
    login: str
    display_name: str | None
    email: str | None
    local: bool
    directory: str | None
    effective: Effective
    session_id: uuid.UUID | None = None

    def has(self, *roles: str) -> bool:
        return self.effective.has(*roles)

    def public(self) -> dict[str, Any]:
        return {"login": self.login, "display_name": self.display_name, "email": self.email,
                "local": self.local, "directory": self.directory, **self.effective.to_json()}


def cli_actor() -> str:
    try:
        who = getpass.getuser()
    except Exception:
        who = "unknown"
    return f"cli:{who}"


def _aad(directory_id: uuid.UUID) -> uuid.UUID:
    """Directory secrets are bound to a namespaced id: a data source's ciphertext (bound to
    its own id) can never decrypt as a directory's, or the other way round."""
    return uuid.uuid5(_SECRET_NS, str(directory_id))


DirectoryFactory = Callable[[Directory, str], Any]  # (row, service password) -> provider


def default_directory_factory(row: Directory, password: str):
    if row.kind == "ldap":
        return LdapDirectory(LdapSettings.model_validate(row.settings), password)
    if row.kind == "entra":
        from gaia.auth.entra import EntraDirectory, EntraSettings

        return EntraDirectory(EntraSettings.model_validate(row.settings), password)
    raise AuthError(f"unknown directory kind {row.kind!r}")


class AuthService:
    def __init__(self, settings: Settings, session_factory, *,
                 directory_factory: DirectoryFactory | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings
        self.cfg = settings.auth
        self.factory = session_factory
        self.directory_factory = directory_factory or default_directory_factory
        self.now = clock or (lambda: datetime.now(UTC))

    # -- helpers -------------------------------------------------------------------------
    async def _directory(self, s: AsyncSession, name: str) -> Directory:
        row = await s.scalar(select(Directory).where(Directory.name == name))
        if row is None:
            raise AuthError(f"no directory {name!r}")
        return row

    def _provider(self, row: Directory):
        if row.secret is None:
            raise AuthError(f"directory {row.name!r} has no service-account password")
        password = decrypt(bytes(row.secret), row.secret_key_id or "", _aad(row.id))
        return self.directory_factory(row, password)

    async def _usable_directories(self, s: AsyncSession) -> list[Directory]:
        return list((await s.scalars(select(Directory).where(
            Directory.status == "verified").order_by(Directory.name))).all())

    async def _held(self, s: AsyncSession, account_id: uuid.UUID) -> list[Held]:
        rows = await s.execute(
            select(Grant.privilege, Grant.data_source_id, DirectoryRole.name)
            .outerjoin(DirectoryRole, DirectoryRole.id == Grant.directory_role_id)
            .where(Grant.revoked_at.is_(None))
            .where((Grant.account_id == account_id) | Grant.directory_role_id.in_(
                select(AccountRole.directory_role_id).where(AccountRole.account_id == account_id)))
            .where(DirectoryRole.gone_at.is_(None) | Grant.account_id.is_not(None)))
        return [Held(p, str(ds) if ds else None, f"role {role}" if role else "account")
                for p, ds, role in rows.all()]

    async def effective_for(self, s: AsyncSession, account: Account) -> Effective:
        if account.is_local:
            return effective([], local=True)
        return effective(await self._held(s, account.id))

    async def _apply_directory_user(self, s: AsyncSession, row: Directory, user: DirectoryUser,
                                    account: Account | None = None) -> Account:
        """Upsert the account and replace its directory roles with what the directory says."""
        now = self.now()
        if account is None:
            account = await s.scalar(select(Account).where(
                Account.directory_id == row.id, Account.login == user.login))
        if account is None:
            account = Account(directory_id=row.id, login=user.login, is_local=False)
            s.add(account)
        account.external_id = user.external_id
        account.display_name = user.display_name
        account.email = user.email
        account.enabled = user.enabled
        account.synced_at = now
        await s.flush()
        role_ids = [(await self._role(s, row.id, r, now)).id for r in user.roles]
        await s.execute(AccountRole.__table__.delete().where(
            AccountRole.account_id == account.id))
        for rid in dict.fromkeys(role_ids):
            s.add(AccountRole(account_id=account.id, directory_role_id=rid, seen_at=now))
        await s.flush()
        return account

    async def _role(self, s: AsyncSession, directory_id: uuid.UUID, ref: RoleRef,
                    now: datetime, members: int | None = None) -> DirectoryRole:
        role = await s.scalar(select(DirectoryRole).where(
            DirectoryRole.directory_id == directory_id, DirectoryRole.source == ref.source,
            DirectoryRole.ref == ref.ref))
        if role is None:
            role = DirectoryRole(directory_id=directory_id, source=ref.source, ref=ref.ref,
                                 name=ref.name, first_seen_at=now)
            s.add(role)
        role.name, role.last_seen_at, role.gone_at = ref.name, now, None
        if ref.description:
            role.description = ref.description
        if members is not None:
            role.members = members
        await s.flush()
        return role

    # -- sign-in and sessions ------------------------------------------------------------
    async def sign_in(self, login: str, password: str, *, directory: str | None = None,
                      ip: str | None = None, user_agent: str | None = None
                      ) -> tuple[Principal, str, str]:
        """(principal, session token, CSRF token) — or :class:`SignInRefused`."""
        login = (login or "").strip()
        async with self.factory() as s:
            try:
                principal = await self._sign_in(s, login, password, directory, ip)
            except SignInRefused as exc:
                await self._refused(s, exc.actor or login.lower() or None, directory, exc, ip)
                raise
            return await self._open_session(s, principal, ip, user_agent, method="password")

    async def _refused(self, s: AsyncSession, actor: str | None, directory: str | None,
                       exc: SignInRefused, ip: str | None) -> None:
        await s.rollback()
        await audit.record(s, "sign_in_refused", actor=actor, subject=directory,
                           detail={"reason": exc.reason}, ip=ip)
        await s.commit()

    async def _open_session(self, s: AsyncSession, principal: Principal, ip: str | None,
                            user_agent: str | None, *, method: str
                            ) -> tuple[Principal, str, str]:
        token, csrf = new_token(), new_token()
        now = self.now()
        row = SessionRow(token_hash=token_hash(token), csrf_hash=token_hash(csrf),
                         account_id=principal.account_id,
                         privileges=principal.effective.to_json(), created_at=now,
                         last_seen_at=now, rechecked_at=now,
                         expires_at=now + timedelta(hours=self.cfg.session_max_hours),
                         ip=ip, user_agent=(user_agent or "")[:300] or None)
        s.add(row)
        await s.flush()
        principal.session_id = row.id
        await audit.record(s, "sign_in", actor=principal.login, subject=principal.directory,
                           detail={"local": principal.local, "method": method,
                                   **principal.effective.to_json()}, ip=ip)
        await s.commit()
        return principal, token, csrf

    # -- OpenID Connect: Entra ID (5A.2) ---------------------------------------------------
    async def oidc_start(self, directory: str, *, ip: str | None = None) -> tuple[str, str]:
        """(the URL to send the browser to, the browser-binding token for its cookie).

        The binding ties the sign-in to the browser that started it: a callback arriving
        in another browser (someone else's code, login CSRF) is refused."""
        async with self.factory() as s:
            row = await s.scalar(select(Directory).where(Directory.name == directory))
            if row is None or row.kind != "entra" or row.status != "verified":
                raise AuthError(f"no usable Entra ID directory {directory!r}")
            state, binding, nonce, verifier = new_token(), new_token(), new_token(), new_token()
            pending = PendingLogin(id=uuid.uuid4(), directory_id=row.id,
                                   state_hash=token_hash(state), binding_hash=token_hash(binding),
                                   nonce=nonce, verifier=b"", verifier_key_id="", ip=ip,
                                   created_at=self.now(),
                                   expires_at=self.now() + timedelta(
                                       minutes=self.cfg.oidc_minutes))
            pending.verifier, pending.verifier_key_id = encrypt(verifier, _aad(pending.id))
            provider = self._provider(row)
            try:
                url = await asyncio.to_thread(provider.authorize_url, state=state, nonce=nonce,
                                              verifier=verifier)
            except DirectoryError as exc:
                raise AuthError(str(exc)) from None
            s.add(pending)
            # expired attempts are of no use to anyone: cleared as new ones start
            await s.execute(PendingLogin.__table__.delete().where(
                PendingLogin.expires_at < self.now() - timedelta(days=1)))
            await s.commit()
            return url, binding

    async def oidc_finish(self, *, state: str | None, code: str | None, binding: str | None,
                          error: str | None = None, ip: str | None = None,
                          user_agent: str | None = None) -> tuple[Principal, str, str, str]:
        """(principal, session token, CSRF token, where the browser goes next) — or
        :class:`SignInRefused` with the real reason in the audit."""
        async with self.factory() as s:
            pending = await s.scalar(select(PendingLogin).where(
                PendingLogin.state_hash == token_hash(state or "")))
            row = await s.get(Directory, pending.directory_id) if pending else None
            name = row.name if row else None
            try:
                if pending is None or row is None:
                    raise SignInRefused("unknown or already used sign-in state")
                if pending.used_at is not None:
                    raise SignInRefused("this sign-in was already completed (a replay)")
                pending.used_at = self.now()
                await s.commit()  # single use, whatever follows
                if pending.expires_at <= self.now():
                    raise SignInRefused("the sign-in took too long (expired)")
                if not same_token(binding, pending.binding_hash):
                    raise SignInRefused("the sign-in came back to another browser")
                if error:
                    raise SignInRefused(f"Microsoft refused: {error[:200]}")
                if not code:
                    raise SignInRefused("no authorization code")
                if row.status != "verified":
                    raise SignInRefused(f"directory {row.name} is {row.status}")
                verifier = decrypt(bytes(pending.verifier), pending.verifier_key_id,
                                   _aad(pending.id))
                try:
                    user = await asyncio.to_thread(self._provider(row).redeem, code,
                                                   verifier=verifier, nonce=pending.nonce)
                except DirectoryError as exc:
                    raise SignInRefused(f"directory {row.name}: {exc}") from None
                principal = await self._admit(s, row, user)
            except SignInRefused as exc:
                await self._refused(s, exc.actor, name, exc, ip)
                raise
            after = str((row.settings or {}).get("after_sign_in") or "/")
            principal, token, csrf = await self._open_session(s, principal, ip, user_agent,
                                                              method="oidc")
            return principal, token, csrf, after

    async def _locked(self, s: AsyncSession, account: Account | None) -> None:
        if account is not None and account.locked_until and account.locked_until > self.now():
            raise SignInRefused("locked after repeated failures")

    async def _failed(self, s: AsyncSession, account: Account | None, reason: str) -> None:
        if account is not None:
            account.failed_sign_ins += 1
            if account.failed_sign_ins >= self.cfg.max_failed_sign_ins:
                account.locked_until = self.now() + timedelta(minutes=self.cfg.lockout_minutes)
                account.failed_sign_ins = 0
                reason += "; account locked"
            await s.commit()  # the counter survives the refusal
        raise SignInRefused(reason)

    async def _sign_in(self, s: AsyncSession, login: str, password: str,
                       directory: str | None, ip: str | None) -> Principal:
        if not login or not password:
            raise SignInRefused("empty login or password")
        if login.lower() == LOCAL_LOGIN and directory is None:
            account = await s.scalar(select(Account).where(Account.is_local))
            if not self.cfg.local_admin_enabled or account is None or not account.enabled:
                verify_password(None, password)  # the same time as a real check
                raise SignInRefused("the local account is disabled or not set up")
            await self._locked(s, account)
            if not verify_password(account.password_hash, password):
                await self._failed(s, account, "wrong password (local account)")
            if account.password_hash and needs_rehash(account.password_hash):
                account.password_hash = hash_password(password)
            account.failed_sign_ins, account.last_sign_in_at = 0, self.now()
            return Principal(account.id, account.login, account.display_name, None, True, None,
                             effective([], local=True))

        # a password goes only to an LDAP directory; Entra ID signs in at Microsoft (5A.2)
        rows = [r for r in await self._usable_directories(s) if r.kind == "ldap"]
        if directory is not None:
            rows = [r for r in rows if r.name == directory]
        if len(rows) != 1:
            raise SignInRefused("no usable directory for a password" if not rows
                                else "several directories: name one")
        row = rows[0]
        existing = await s.scalar(select(Account).where(
            Account.directory_id == row.id, Account.login == login.lower()))
        await self._locked(s, existing)
        provider = self._provider(row)
        try:
            user = await asyncio.to_thread(provider.authenticate, login, password)
        except DirectoryError as exc:
            raise SignInRefused(f"directory {row.name}: {exc}") from None
        if user is None:
            await self._failed(s, existing, "wrong password or unknown login")
        return await self._admit(s, row, user)

    async def _admit(self, s: AsyncSession, row: Directory, user: DirectoryUser) -> Principal:
        """The same rules for every directory once it has vouched for the person."""
        # the account is kept even when the sign-in is refused below: the administrator
        # then sees who tried, with their directory roles, and can grant (or not)
        account = await self._apply_directory_user(s, row, user)
        await s.commit()
        if not user.enabled:
            raise SignInRefused("disabled in the directory", account.login)
        try:
            await self._locked(s, account)  # the same person by another spelling of the login
        except SignInRefused as exc:
            exc.actor = account.login
            raise
        eff = await self.effective_for(s, account)
        if not eff.can_sign_in:
            raise SignInRefused("no Gaia role (no grant to the account or its directory roles)",
                                account.login)
        account.failed_sign_ins, account.last_sign_in_at = 0, self.now()
        return Principal(account.id, account.login, account.display_name, account.email,
                         False, row.name, eff)

    async def resolve(self, token: str | None, *, csrf: str | None = None,
                      unsafe: bool = False, ip: str | None = None) -> Principal:
        """The principal of a session cookie. ``unsafe`` requests (anything but GET/HEAD)
        must carry the session's CSRF token too."""
        if not token:
            raise NotSignedIn("not signed in")
        async with self.factory() as s:
            row = await s.scalar(select(SessionRow).where(
                SessionRow.token_hash == token_hash(token)))
            now = self.now()
            if row is None or row.revoked_at is not None:
                raise NotSignedIn("not signed in")
            idle = timedelta(minutes=self.cfg.session_idle_minutes)
            if now >= row.expires_at or now - row.last_seen_at >= idle:
                await self._end(s, row, "expired")
                raise NotSignedIn("the session has expired")
            if unsafe and not same_token(csrf, row.csrf_hash):
                raise NotSignedIn("missing or wrong CSRF token")
            account = await s.get(Account, row.account_id)
            if account is None or not account.enabled:
                await self._end(s, row, "account disabled")
                raise NotSignedIn("the account is disabled")
            directory = await s.get(Directory, account.directory_id) if account.directory_id \
                else None
            eff = Effective.from_json(row.privileges)
            due = now - row.rechecked_at >= timedelta(minutes=self.cfg.recheck_minutes)
            retry_at = row.privileges.get("retry_at")
            if due and not account.is_local and (retry_at is None or retry_at <= now.isoformat()):
                eff = await self._recheck(s, row, account, directory, now)
            if now - row.last_seen_at >= timedelta(seconds=60):
                row.last_seen_at = now
            await s.commit()
            return Principal(account.id, account.login, account.display_name, account.email,
                             account.is_local, directory.name if directory else None, eff, row.id)

    async def _recheck(self, s: AsyncSession, row: SessionRow, account: Account,
                       directory: Directory | None, now: datetime) -> Effective:
        if directory is None or directory.status == "disabled":
            await self._end(s, row, "directory disabled")
            raise NotSignedIn("the directory is disabled")
        try:
            user = await asyncio.to_thread(self._provider(directory).lookup, account.login,
                                           account.external_id)
        except (DirectoryError, AuthError) as exc:
            if now - row.rechecked_at >= 2 * timedelta(minutes=self.cfg.recheck_minutes):
                await self._end(s, row, f"directory unreachable: {exc}")
                raise NotSignedIn("the directory could not confirm the account") from None
            row.privileges = {**row.privileges,
                              "retry_at": (now + timedelta(seconds=RETRY_S)).isoformat()}
            return Effective.from_json(row.privileges)
        if user is None or not user.enabled:
            account.enabled = user is not None and user.enabled
            await self._end(s, row, "gone or disabled in the directory")
            raise NotSignedIn("the directory no longer allows this account")
        await self._apply_directory_user(s, directory, user, account)
        eff = await self.effective_for(s, account)
        if not eff.can_sign_in:
            await self._end(s, row, "no Gaia role any more")
            raise NotSignedIn("no Gaia role any more")
        if eff.to_json() != Effective.from_json(row.privileges).to_json():
            await audit.record(s, "privileges_changed", actor=account.login,
                               subject=directory.name, detail=eff.to_json())
        row.privileges, row.rechecked_at = eff.to_json(), now
        return eff

    async def _end(self, s: AsyncSession, row: SessionRow, reason: str) -> None:
        row.revoked_at, row.revoked_reason = self.now(), reason
        account = await s.get(Account, row.account_id)
        await audit.record(s, "session_ended", actor=account.login if account else None,
                           detail={"reason": reason})
        await s.commit()

    async def sign_out(self, token: str | None) -> None:
        if not token:
            return
        async with self.factory() as s:
            row = await s.scalar(select(SessionRow).where(
                SessionRow.token_hash == token_hash(token)))
            if row is not None and row.revoked_at is None:
                row.revoked_at, row.revoked_reason = self.now(), "signed out"
                account = await s.get(Account, row.account_id)
                await audit.record(s, "sign_out", actor=account.login if account else None)
                await s.commit()

    # -- the local account (D68) ---------------------------------------------------------
    async def _local(self, s: AsyncSession, create: bool = False) -> Account | None:
        account = await s.scalar(select(Account).where(Account.is_local))
        if account is None and create:
            account = Account(login=LOCAL_LOGIN, is_local=True, display_name="Gaia administrator")
            s.add(account)
            await s.flush()
        return account

    async def set_local_password(self, password: str, *, actor: str) -> None:
        check_policy(password, LOCAL_LOGIN)
        async with self.factory() as s:
            account = await self._local(s, create=True)
            account.password_hash = hash_password(password)
            account.password_set_at = self.now()
            account.setup_code_hash = account.setup_code_expires_at = None
            account.failed_sign_ins, account.locked_until = 0, None
            await self._end_sessions(s, account.id, "password changed")
            await audit.record(s, "local_password_set", actor=actor, subject=LOCAL_LOGIN)
            await s.commit()

    async def issue_setup_code(self, *, actor: str) -> str:
        """A one-time code for the local account's first (or a lost) password; whoever can
        run the CLI on the server is trusted with it (D68)."""
        code = new_token()
        async with self.factory() as s:
            account = await self._local(s, create=True)
            account.setup_code_hash = token_hash(code)
            account.setup_code_expires_at = self.now() + timedelta(
                minutes=self.cfg.setup_code_minutes)
            await audit.record(s, "local_setup_code_issued", actor=actor, subject=LOCAL_LOGIN,
                               detail={"valid_minutes": self.cfg.setup_code_minutes})
            await s.commit()
        return code

    async def complete_setup(self, code: str, password: str, *, ip: str | None = None) -> None:
        async with self.factory() as s:
            account = await self._local(s)
            valid = (account is not None and account.setup_code_expires_at is not None
                     and account.setup_code_expires_at > self.now()
                     and same_token(code, account.setup_code_hash))
            if not valid:
                await audit.record(s, "local_setup_refused", actor=None, subject=LOCAL_LOGIN,
                                   ip=ip)
                await s.commit()
                raise AuthError("the setup code is wrong or has expired (make admin-user "
                                "ARGS=setup-code prints a new one)")
        try:
            check_policy(password, LOCAL_LOGIN)
        except PasswordPolicyError as exc:
            raise AuthError(str(exc)) from None
        await self.set_local_password(password, actor=f"setup code ({ip or 'unknown'})")

    async def set_local_enabled(self, enabled: bool, *, actor: str) -> None:
        async with self.factory() as s:
            account = await self._local(s, create=True)
            account.enabled = enabled
            if not enabled:
                await self._end_sessions(s, account.id, "local account disabled")
            await audit.record(s, "local_enabled" if enabled else "local_disabled", actor=actor,
                               subject=LOCAL_LOGIN)
            await s.commit()

    async def local_status(self) -> dict[str, Any]:
        async with self.factory() as s:
            account = await self._local(s)
            if account is None:
                return {"exists": False, "enabled": False, "password_set": False}
            return {"exists": True, "enabled": account.enabled,
                    "password_set": account.password_hash is not None,
                    "setup_code_pending": bool(account.setup_code_expires_at
                                               and account.setup_code_expires_at > self.now()),
                    "last_sign_in_at": account.last_sign_in_at}

    async def _end_sessions(self, s: AsyncSession, account_id: uuid.UUID, reason: str) -> None:
        await s.execute(update(SessionRow).where(
            SessionRow.account_id == account_id, SessionRow.revoked_at.is_(None)
        ).values(revoked_at=self.now(), revoked_reason=reason))

    # -- directories -----------------------------------------------------------------------
    def _validate(self, kind: str, settings: dict[str, Any]) -> dict[str, Any]:
        if kind == "ldap":
            try:
                return LdapSettings.model_validate(settings).model_dump(exclude_none=True)
            except ValueError as exc:
                raise AuthError(f"directory settings: {exc}") from None
        if kind == "entra":
            from gaia.auth.entra import EntraSettings

            try:
                return EntraSettings.model_validate(settings).model_dump(exclude_none=True)
            except ValueError as exc:
                raise AuthError(f"directory settings: {exc}") from None
        raise AuthError(f"unknown directory kind {kind!r} (ldap | entra)")

    async def add_directory(self, name: str, kind: str, settings: dict[str, Any],
                            password: str, *, actor: str) -> dict[str, Any]:
        name = (name or "").strip()
        if not name or not password:
            raise AuthError("a directory needs a name and the service account's password")
        clean = self._validate(kind, settings)
        async with self.factory() as s:
            if await s.scalar(select(Directory).where(Directory.name == name)):
                raise AuthError(f"a directory named {name!r} exists")
            row = Directory(id=uuid.uuid4(), name=name, kind=kind, settings=clean,
                            status="unverified", created_by=actor)
            row.secret, row.secret_key_id = encrypt(password, _aad(row.id))
            s.add(row)
            await s.flush()
            await audit.record(s, "directory_added", actor=actor, subject=name,
                               detail={"kind": kind, "settings": clean})
            await s.commit()
            return _directory_view(row)

    async def update_directory(self, name: str, *, actor: str,
                               settings: dict[str, Any] | None = None,
                               password: str | None = None) -> dict[str, Any]:
        async with self.factory() as s:
            row = await self._directory(s, name)
            changed = []
            if settings is not None:
                row.settings = self._validate(row.kind, {**row.settings, **settings})
                changed += sorted(settings)
            if password:
                row.secret, row.secret_key_id = encrypt(password, _aad(row.id))
                changed.append("service account password")
            if changed:  # any change must be proven again, as for data sources (D65)
                row.status, row.probed_at = "unverified", None
            await audit.record(s, "directory_changed", actor=actor, subject=name,
                               detail={"changed": changed})
            await s.commit()
            return _directory_view(row)

    async def probe_directory(self, name: str, *, actor: str) -> dict[str, Any]:
        async with self.factory() as s:
            row = await self._directory(s, name)
            try:
                checks = await asyncio.to_thread(self._provider(row).probe)
            except (AuthError, DirectoryError) as exc:
                checks = [{"name": "provider", "ok": False, "detail": str(exc)}]
            row.probe, row.probed_at = checks, self.now()
            if row.status != "disabled":
                row.status = "verified" if checks and all(c["ok"] for c in checks) else "failed"
            await audit.record(s, "directory_probed", actor=actor, subject=name,
                               detail={"status": row.status,
                                       "failed": [c["name"] for c in checks if not c["ok"]]})
            await s.commit()
            return _directory_view(row)

    async def set_directory_enabled(self, name: str, enabled: bool, *, actor: str
                                    ) -> dict[str, Any]:
        async with self.factory() as s:
            row = await self._directory(s, name)
            row.status = "unverified" if enabled else "disabled"
            if not enabled:
                ids = select(Account.id).where(Account.directory_id == row.id)
                await s.execute(update(SessionRow).where(
                    SessionRow.account_id.in_(ids), SessionRow.revoked_at.is_(None)
                ).values(revoked_at=self.now(), revoked_reason="directory disabled"))
            await audit.record(s, "directory_enabled" if enabled else "directory_disabled",
                               actor=actor, subject=name)
            await s.commit()
            return _directory_view(row)

    async def sync_directory(self, name: str, *, actor: str) -> dict[str, Any]:
        """Every user and role the directory has under the administrator's settings: the
        distinct list of roles for the role mapping, and who holds which (P5-3)."""
        async with self.factory() as s:
            row = await self._directory(s, name)
            if row.status != "verified":
                raise AuthError(f"directory {name!r} is {row.status}: probe it first")
            try:
                result = await asyncio.to_thread(self._provider(row).sync)
            except DirectoryError as exc:
                raise AuthError(f"sync failed: {exc}") from None
            now = self.now()
            for ref, members in result.roles.items():
                await self._role(s, row.id, ref, now, members)
            seen = {(r.source, r.ref) for r in result.roles}
            gone = 0
            for role in (await s.scalars(select(DirectoryRole).where(
                    DirectoryRole.directory_id == row.id, DirectoryRole.gone_at.is_(None)))).all():
                if (role.source, role.ref) not in seen:
                    role.gone_at, gone = now, gone + 1
            logins = set()
            for user in result.users:
                await self._apply_directory_user(s, row, user)
                logins.add(user.login)
            missing = (await s.scalars(select(Account).where(
                Account.directory_id == row.id, Account.enabled.is_(True),
                Account.login.not_in(logins or {""})))).all()
            for account in missing:
                account.enabled = False
                await self._end_sessions(s, account.id, "not in the directory any more")
            row.synced_at = now
            row.sync = {"users": len(result.users), "roles": len(result.roles),
                        "roles_gone": gone, "accounts_disabled": len(missing),
                        "disabled_in_directory": sum(1 for u in result.users if not u.enabled)}
            await audit.record(s, "directory_synced", actor=actor, subject=name, detail=row.sync)
            await s.commit()
            return _directory_view(row)

    async def directories(self) -> list[dict[str, Any]]:
        async with self.factory() as s:
            return [_directory_view(r) for r in
                    (await s.scalars(select(Directory).order_by(Directory.name))).all()]

    async def sign_in_options(self) -> dict[str, Any]:
        """What the sign-in screen offers (public): usable directories, and whether the
        local account can sign in."""
        async with self.factory() as s:
            rows = await self._usable_directories(s)
            local = await self._local(s)
            return {"directories": [{"name": r.name, "kind": r.kind} for r in rows],  # entra:
                    # a "sign in with Microsoft" button (GET /api/auth/oidc/start?directory=…)
                    "local": bool(self.cfg.local_admin_enabled and local and local.enabled
                                  and local.password_hash),
                    "local_setup_pending": bool(local and local.setup_code_expires_at
                                                and local.setup_code_expires_at > self.now())}

    async def roles(self, name: str, *, include_gone: bool = False) -> list[dict[str, Any]]:
        async with self.factory() as s:
            row = await self._directory(s, name)
            q = select(DirectoryRole).where(DirectoryRole.directory_id == row.id)
            if not include_gone:
                q = q.where(DirectoryRole.gone_at.is_(None))
            return [_role_view(r, name) for r in
                    (await s.scalars(q.order_by(DirectoryRole.source, DirectoryRole.name))).all()]

    async def rotate_secrets(self) -> int:
        """Re-encrypt every directory secret with the current master key (D65's rotation)."""
        async with self.factory() as s:
            n = 0
            for row in (await s.scalars(select(Directory).where(
                    Directory.secret.is_not(None)))).all():
                plain = decrypt(bytes(row.secret), row.secret_key_id or "", _aad(row.id))
                blob, kid = encrypt(plain, _aad(row.id))
                if kid != row.secret_key_id:
                    row.secret, row.secret_key_id, n = blob, kid, n + 1
                    await audit.record(s, "directory_secret_rotated", actor=cli_actor(),
                                       subject=row.name, detail={"key_id": kid})
            await s.commit()
            return n

    # -- grants (D68, D70: the administrator decides, the auditor reads) -------------------
    async def _subject(self, s: AsyncSession, *, directory_role_id: uuid.UUID | None,
                       role: tuple[str, str] | None, account: tuple[str | None, str] | None
                       ) -> tuple[DirectoryRole | None, Account | None]:
        if directory_role_id is not None:
            r = await s.get(DirectoryRole, directory_role_id)
            if r is None:
                raise AuthError("no such directory role")
            return r, None
        if role is not None:
            directory, name = role
            row = await self._directory(s, directory)
            found = (await s.scalars(select(DirectoryRole).where(
                DirectoryRole.directory_id == row.id, DirectoryRole.gone_at.is_(None),
                func.lower(DirectoryRole.name) == name.lower()))).all()
            if len(found) != 1:
                raise AuthError(f"{len(found)} directory role(s) named {name!r} in {directory!r}"
                                " (sync the directory, or give the role's id)")
            return found[0], None
        if account is not None:
            directory, login = account
            if directory is None and login.strip().lower() == LOCAL_LOGIN:
                raise AuthError("the local account is an administrator and nothing else")
            q = select(Account).where(Account.login == login.lower())
            if directory is None:
                q = q.where(Account.is_local.is_(False))
            else:
                q = q.where(Account.directory_id == (await self._directory(s, directory)).id)
            found = (await s.scalars(q)).all()
            if len(found) != 1:
                raise AuthError(f"{len(found)} account(s) with login {login!r}"
                                + ("" if directory else " (name the directory)"))
            if found[0].is_local:
                raise AuthError("the local account is an administrator and nothing else")
            return None, found[0]
        raise AuthError("a grant needs a directory role or an account")

    async def grant(self, privilege: str, *, actor: str,
                    directory_role_id: uuid.UUID | None = None,
                    role: tuple[str, str] | None = None,
                    account: tuple[str | None, str] | None = None,
                    data_source: str | None = None, note: str | None = None) -> dict[str, Any]:
        if privilege not in PRIVILEGES:
            raise AuthError(f"unknown privilege {privilege!r} ({', '.join(PRIVILEGES)})")
        if (privilege == "sme") != bool(data_source):
            raise AuthError("sme is granted per data source (and only sme is)")
        async with self.factory() as s:
            r, a = await self._subject(s, directory_role_id=directory_role_id, role=role,
                                       account=account)
            ds_id = None
            if data_source:
                ds = await s.scalar(select(DataSource).where(DataSource.name == data_source))
                if ds is None or ds.status == "tombstoned":
                    raise AuthError(f"no data source {data_source!r}")
                ds_id = ds.id
            same_subject = (Grant.directory_role_id == r.id) if r is not None \
                else (Grant.account_id == a.id)
            same_source = Grant.data_source_id.is_(None) if ds_id is None \
                else (Grant.data_source_id == ds_id)
            existing = await s.scalar(select(Grant).where(
                Grant.revoked_at.is_(None), Grant.privilege == privilege, same_subject,
                same_source))
            if existing is not None:
                raise AuthError("this grant exists already")
            g = Grant(privilege=privilege, directory_role_id=r.id if r else None,
                      account_id=a.id if a else None, data_source_id=ds_id,
                      granted_by=actor, granted_at=self.now(), note=note)
            s.add(g)
            await s.flush()
            view = await _grant_view(s, g)
            await audit.record(s, "grant_added", actor=actor, subject=view["subject"],
                               detail={"privilege": privilege, "data_source": data_source,
                                       "grant_id": str(g.id), "note": note})
            await s.commit()
            return view

    async def revoke(self, grant_id: uuid.UUID, *, actor: str, note: str | None = None
                     ) -> dict[str, Any]:
        async with self.factory() as s:
            g = await s.get(Grant, grant_id)
            if g is None or g.revoked_at is not None:
                raise AuthError("no such current grant")
            g.revoked_at, g.revoked_by = self.now(), actor
            view = await _grant_view(s, g)
            await audit.record(s, "grant_revoked", actor=actor, subject=view["subject"],
                               detail={"privilege": g.privilege, "grant_id": str(g.id),
                                       "note": note})
            await s.commit()
            return view

    async def grants(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        async with self.factory() as s:
            q = select(Grant).order_by(Grant.granted_at)
            if not include_revoked:
                q = q.where(Grant.revoked_at.is_(None))
            return [await _grant_view(s, g) for g in (await s.scalars(q)).all()]

    async def accounts(self) -> list[dict[str, Any]]:
        """Every account, its directory roles and what it may do now (the grants applied,
        conflicts included) — the administrator's and the auditor's view."""
        async with self.factory() as s:
            out = []
            for a in (await s.scalars(select(Account).order_by(Account.is_local.desc(),
                                                                Account.login))).all():
                roles = (await s.scalars(select(DirectoryRole.name).join(
                    AccountRole, AccountRole.directory_role_id == DirectoryRole.id).where(
                    AccountRole.account_id == a.id).order_by(DirectoryRole.name))).all()
                directory = await s.get(Directory, a.directory_id) if a.directory_id else None
                eff = await self.effective_for(s, a)
                names = await _source_names(s, eff.sme_sources)
                out.append({"login": a.login, "directory": directory.name if directory else None,
                            "local": a.is_local, "display_name": a.display_name,
                            "email": a.email, "enabled": a.enabled,
                            "last_sign_in_at": a.last_sign_in_at, "synced_at": a.synced_at,
                            "directory_roles": list(roles),
                            **eff.to_json(), "sme_sources": names})
            return out


def _directory_view(row: Directory) -> dict[str, Any]:
    return {"name": row.name, "kind": row.kind, "status": row.status, "settings": row.settings,
            "probed_at": row.probed_at, "probe": row.probe, "synced_at": row.synced_at,
            "sync": row.sync, "created_by": row.created_by}


def _role_view(r: DirectoryRole, directory: str) -> dict[str, Any]:
    return {"id": str(r.id), "directory": directory, "source": r.source, "name": r.name,
            "ref": r.ref, "description": r.description, "members": r.members,
            "last_seen_at": r.last_seen_at, "gone_at": r.gone_at}


async def _source_names(s: AsyncSession, ids) -> list[str]:
    if not ids:
        return []
    rows = await s.scalars(select(DataSource.name).where(
        DataSource.id.in_([uuid.UUID(str(i)) for i in ids])))
    return sorted(rows.all())


async def _grant_view(s: AsyncSession, g: Grant) -> dict[str, Any]:
    if g.directory_role_id:
        r = await s.get(DirectoryRole, g.directory_role_id)
        d = await s.get(Directory, r.directory_id) if r else None
        subject = f"role {d.name if d else '?'}/{r.name if r else '?'}"
    else:
        a = await s.get(Account, g.account_id)
        d = await s.get(Directory, a.directory_id) if a and a.directory_id else None
        subject = f"account {d.name + '/' if d else ''}{a.login if a else '?'}"
    ds = await s.get(DataSource, g.data_source_id) if g.data_source_id else None
    return {"id": str(g.id), "privilege": g.privilege, "subject": subject,
            "data_source": ds.name if ds else None, "granted_by": g.granted_by,
            "granted_at": g.granted_at, "note": g.note, "revoked_by": g.revoked_by,
            "revoked_at": g.revoked_at}


assert set(APP_ROLES) <= set(PRIVILEGES)
