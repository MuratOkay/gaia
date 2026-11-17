"""Active Directory / LDAP(S) as Gaia's identity provider (Phase 5A, D68).

**Sign-in.** The service account finds the person (the login is escaped into the
filter; ``DOMAIN\\user`` and ``user@domain`` are understood), then *the person's own bind*
checks the password — Gaia never compares or keeps directory passwords. An empty password
is refused before anything is sent: a simple bind with an empty password is an
*anonymous* bind, which AD accepts.

**Transport.** ``ldaps://`` or ``ldap://`` with StartTLS, nothing else: a clear-text URL is
refused when the settings are validated. The certificate is verified (``ca_file`` for a
private CA); ``tls_verify: false`` exists for test directories and the probe says so.

**Roles — the administrator chooses how they are read (P5-3):**

* ``group`` — membership of the groups under ``group_base`` matching ``group_filter``;
  nested membership followed upwards through *any* group (an intermediate group need not
  be a role). ``nested_strategy: client`` walks the graph with plain searches (any LDAP
  server); ``ad_in_chain`` asks AD in one query (``LDAP_MATCHING_RULE_IN_CHAIN``);
* ``attribute`` — every value of one user attribute (``department``, ``title``,
  ``employeeType``, ``extensionAttribute5`` …);
* ``ou`` — the organisational units a user sits in, below ``ou_base``.

**Sync** reads every user under ``user_base`` and the roles they have, for the admin
screens (the distinct list of roles, who has which). For groups it reads the group graph
once and resolves nesting in memory rather than per user.

**Probe** — the "test connection" of the admin screen, like the data-source probe: TLS,
service bind, users found, roles found.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

__all__ = ["DirectoryError", "DirectoryUser", "LdapDirectory", "LdapSettings", "RoleRef",
           "RoleSource", "SyncResult"]

AD_USER_FILTER = "(&(objectCategory=person)(objectClass=user))"
AD_GROUP_CLASS = "(objectClass=group)"
LDAP_GROUP_CLASS = "(|(objectClass=groupOfNames)(objectClass=groupOfUniqueNames))"
IN_CHAIN = "1.2.840.113556.1.4.1941"
MAX_DEPTH = 10  # nested groups followed at most this deep (cycles are harmless anyway)
ACCOUNTDISABLE = 0x2


class DirectoryError(RuntimeError):
    """The directory cannot be reached or answered unexpectedly (not: wrong password)."""


class RoleSource(BaseModel):
    kind: Literal["group", "attribute", "ou"] = "group"
    # group
    group_base: str | None = None  # default: base_dn
    group_filter: str | None = None  # default: the flavour's group class
    group_name_attribute: str = "cn"
    member_attribute: str = "member"
    nested: bool = True
    nested_strategy: Literal["client", "ad_in_chain"] = "client"
    # attribute
    attribute: str | None = None
    # ou
    ou_base: str | None = None  # default: user_base

    @model_validator(mode="after")
    def _complete(self) -> RoleSource:
        if self.kind == "attribute" and not self.attribute:
            raise ValueError("role_source.attribute is needed when roles come from an attribute")
        return self


class LdapSettings(BaseModel):
    """Non-secret settings of one directory (``auth.directory.settings``). The service
    account's password is stored encrypted beside them, never here."""

    urls: list[str] = Field(min_length=1)
    start_tls: bool = False
    tls_verify: bool = True
    ca_file: str | None = None
    flavor: Literal["ad", "openldap"] = "ad"
    bind_dn: str
    base_dn: str
    user_base: str | None = None  # default: base_dn
    user_filter: str | None = None  # default: people (AD) / inetOrgPerson
    login_attribute: str | None = None  # default: sAMAccountName (AD) / uid
    display_name_attribute: str = "displayName"
    email_attribute: str = "mail"
    disabled_attribute: str | None = None  # OpenLDAP: present and true means disabled
    role_source: RoleSource = Field(default_factory=RoleSource)
    timeout_s: int = 10

    @model_validator(mode="after")
    def _secure_and_complete(self) -> LdapSettings:
        for url in self.urls:
            low = url.lower()
            if not (low.startswith("ldaps://") or (low.startswith("ldap://") and self.start_tls)):
                raise ValueError(f"{url}: only ldaps:// or ldap:// with start_tls — a clear-text "
                                 "bind would send passwords unencrypted")
        return self

    # effective values
    @property
    def users_at(self) -> str:
        return self.user_base or self.base_dn

    @property
    def people(self) -> str:
        return self.user_filter or (AD_USER_FILTER if self.flavor == "ad"
                                    else "(objectClass=inetOrgPerson)")

    @property
    def login_attr(self) -> str:
        return self.login_attribute or ("sAMAccountName" if self.flavor == "ad" else "uid")

    @property
    def group_class(self) -> str:
        return AD_GROUP_CLASS if self.flavor == "ad" else LDAP_GROUP_CLASS

    @property
    def groups_at(self) -> str:
        return self.role_source.group_base or self.base_dn

    @property
    def role_groups(self) -> str:
        return self.role_source.group_filter or self.group_class

    @property
    def ous_at(self) -> str:
        return self.role_source.ou_base or self.users_at


@dataclass(frozen=True)
class RoleRef:
    source: str  # group | attribute | ou
    ref: str  # lower-case group DN, attribute value or OU DN
    name: str
    description: str | None = None


@dataclass
class DirectoryUser:
    login: str  # the login attribute's value, lower case
    dn: str
    external_id: str | None
    display_name: str | None
    email: str | None
    enabled: bool
    roles: list[RoleRef] = field(default_factory=list)


@dataclass
class SyncResult:
    users: list[DirectoryUser]
    roles: dict[RoleRef, int]  # role -> members


Connector = Callable[[str, str], Any]  # (dn, password) -> a bound ldap3 Connection or None


def _first(entry: dict[str, Any], name: str | None) -> str | None:
    if not name:
        return None
    value = entry.get(name) if isinstance(entry, dict) else None
    if value is None:
        for key, v in (entry or {}).items():  # attribute names are case-insensitive
            if key.lower() == name.lower():
                value = v
                break
    if isinstance(value, list | tuple):
        value = value[0] if value else None
    if isinstance(value, bytes):
        return value.hex()
    return None if value in (None, "") else str(value)


def _all(entry: dict[str, Any], name: str) -> list[str]:
    for key, v in (entry or {}).items():
        if key.lower() == name.lower():
            values = v if isinstance(v, list | tuple) else [v]
            return [str(x) for x in values if x not in (None, "")]
    return []


def _norm(dn: str) -> str:
    return ",".join(part.strip() for part in dn.split(",")).lower()


class LdapDirectory:
    def __init__(self, settings: LdapSettings, service_password: str, *,
                 connect: Connector | None = None) -> None:
        self.s = settings
        self._password = service_password
        self._connect = connect or _tls_connector(settings)

    # -- connections ---------------------------------------------------------------------
    def _service(self):
        try:
            conn = self._connect(self.s.bind_dn, self._password)
        except Exception as exc:  # network, TLS, timeouts
            raise DirectoryError(f"directory unreachable: {type(exc).__name__}: {exc}") from None
        if conn is None:
            raise DirectoryError("the service account's bind was refused (bind_dn or password)")
        return conn

    @staticmethod
    def _close(conn) -> None:
        try:
            conn.unbind()
        except Exception:  # noqa: S110 - closing a connection that is already gone
            pass

    def _search(self, conn, base: str, flt: str, attributes: list[str]) -> list[dict[str, Any]]:
        from ldap3 import SUBTREE

        rows = conn.extend.standard.paged_search(base, flt, SUBTREE, attributes=attributes,
                                                 paged_size=500, generator=False)
        return [{"dn": r["dn"], **(r.get("attributes") or {})}
                for r in rows if r.get("type") == "searchResEntry"]

    # -- users -------------------------------------------------------------------------------
    def _user_filter(self, login: str) -> str:
        from ldap3.utils.conv import escape_filter_chars

        if "\\" in login:  # DOMAIN\user
            login = login.split("\\", 1)[1]
        value = escape_filter_chars(login.strip())
        match = f"({self.s.login_attr}={value})"
        if "@" in login and self.s.flavor == "ad":
            match = f"(|{match}(userPrincipalName={value}))"
        return f"(&{self.s.people}{match})"

    def _attributes(self) -> list[str]:
        extra = ["userAccountControl", "objectGUID"] if self.s.flavor == "ad" else ["entryUUID"]
        attrs = [self.s.login_attr, self.s.display_name_attribute, self.s.email_attribute, *extra]
        if self.s.disabled_attribute:
            attrs.append(self.s.disabled_attribute)
        if self.s.role_source.kind == "attribute":
            attrs.append(self.s.role_source.attribute or "")
        return [a for a in dict.fromkeys(attrs) if a]

    def _to_user(self, entry: dict[str, Any]) -> DirectoryUser:
        s = self.s
        enabled = True
        if s.flavor == "ad":
            uac = _first(entry, "userAccountControl")
            enabled = not (uac is not None and uac.isdigit() and int(uac) & ACCOUNTDISABLE)
        if s.disabled_attribute:
            flag = _first(entry, s.disabled_attribute)
            enabled = enabled and not (flag and flag.lower() not in ("false", "0", "no"))
        return DirectoryUser(
            login=(_first(entry, s.login_attr) or "").lower(),
            dn=entry["dn"],
            external_id=_first(entry, "objectGUID" if s.flavor == "ad" else "entryUUID")
            or entry["dn"].lower(),
            display_name=_first(entry, s.display_name_attribute),
            email=_first(entry, s.email_attribute),
            enabled=enabled)

    def _find(self, conn, login: str) -> dict[str, Any] | None:
        found = self._search(conn, self.s.users_at, self._user_filter(login), self._attributes())
        return found[0] if len(found) == 1 else None  # none, or ambiguous: not a sign-in

    def authenticate(self, login: str, password: str) -> DirectoryUser | None:
        """The person, with their roles, if the directory accepts the password; None when
        the login is unknown, ambiguous or the password is wrong (one answer for all three,
        so a caller cannot tell which)."""
        if not login.strip() or not password:
            return None
        conn = self._service()
        try:
            entry = self._find(conn, login)
            if entry is None:
                return None
            try:
                own = self._connect(entry["dn"], password)
            except Exception as exc:
                raise DirectoryError(f"directory unreachable: {type(exc).__name__}") from None
            if own is None:
                return None
            self._close(own)
            user = self._to_user(entry)
            user.roles = self._roles(conn, entry)
            return user
        finally:
            self._close(conn)

    def lookup(self, login: str, external_id: str | None = None) -> DirectoryUser | None:
        """The person as the directory sees them now (the service account only): for the
        re-check of an open session. (``external_id`` is for providers that look people up by
        a stable id; LDAP finds them by login.)"""
        conn = self._service()
        try:
            entry = self._find(conn, login)
            if entry is None:
                return None
            user = self._to_user(entry)
            user.roles = self._roles(conn, entry)
            return user
        finally:
            self._close(conn)

    # -- roles -------------------------------------------------------------------------------
    def _role_groups(self, conn) -> dict[str, RoleRef]:
        s, rs = self.s, self.s.role_source
        rows = self._search(conn, s.groups_at, s.role_groups,
                            [rs.group_name_attribute, "description"])
        return {_norm(r["dn"]): RoleRef("group", _norm(r["dn"]),
                                        _first(r, rs.group_name_attribute) or r["dn"],
                                        _first(r, "description"))
                for r in rows}

    def _parents(self, conn, dns: Iterable[str]) -> set[str]:
        """Groups (of any kind, anywhere under base_dn) that have one of ``dns`` as a member."""
        from ldap3.utils.conv import escape_filter_chars

        dns, out = list(dns), set()
        member = self.s.role_source.member_attribute
        for i in range(0, len(dns), 50):
            ors = "".join(f"({member}={escape_filter_chars(d)})" for d in dns[i:i + 50])
            rows = self._search(conn, self.s.base_dn, f"(&{self.s.group_class}(|{ors}))", ["cn"])
            out |= {_norm(r["dn"]) for r in rows}
        return out

    def _roles(self, conn, entry: dict[str, Any]) -> list[RoleRef]:
        s, rs = self.s, self.s.role_source
        if rs.kind == "attribute":
            return sorted({RoleRef("attribute", v.strip().lower(), v.strip())
                           for v in _all(entry, rs.attribute or "") if v.strip()},
                          key=lambda r: r.ref)
        if rs.kind == "ou":
            return _ous(entry["dn"], s.ous_at)
        roles = self._role_groups(conn)
        if rs.nested and rs.nested_strategy == "ad_in_chain":
            from ldap3.utils.conv import escape_filter_chars

            flt = f"(&{s.role_groups}({rs.member_attribute}:{IN_CHAIN}:="
            flt += f"{escape_filter_chars(entry['dn'])}))"
            found = {_norm(r["dn"]) for r in self._search(conn, s.groups_at, flt, ["cn"])}
        else:
            found, frontier = set(), {entry["dn"]}
            for _depth in range(MAX_DEPTH if rs.nested else 1):
                parents = self._parents(conn, frontier) - found
                if not parents:
                    break
                found |= parents
                frontier = parents
        return sorted((roles[d] for d in found if d in roles), key=lambda r: r.ref)

    # -- sync --------------------------------------------------------------------------------
    def sync(self) -> SyncResult:
        s, rs = self.s, self.s.role_source
        conn = self._service()
        try:
            entries = self._search(conn, s.users_at, s.people, self._attributes())
            users = [self._to_user(e) for e in entries]
            by_dn = {_norm(e["dn"]): u for e, u in zip(entries, users, strict=True)}
            counts: dict[RoleRef, int] = {}
            if rs.kind in ("attribute", "ou"):
                for e, u in zip(entries, users, strict=True):
                    u.roles = self._roles(conn, e)
            else:
                roles = self._role_groups(conn)
                counts = dict.fromkeys(roles.values(), 0)
                graph = self._search(conn, s.base_dn, s.group_class, [rs.member_attribute])
                parents: dict[str, set[str]] = {}  # member DN -> groups holding it
                for g in graph:
                    for m in _all(g, rs.member_attribute):
                        parents.setdefault(_norm(m), set()).add(_norm(g["dn"]))
                for dn, u in by_dn.items():
                    found, frontier = set(), {dn}
                    for _depth in range(MAX_DEPTH if rs.nested else 1):
                        nxt = set().union(*(parents.get(d, set()) for d in frontier)) - found
                        if not nxt:
                            break
                        found |= nxt
                        frontier = nxt
                    u.roles = sorted((roles[g] for g in found if g in roles),
                                     key=lambda r: r.ref)
            for u in users:
                for r in u.roles:
                    counts[r] = counts.get(r, 0) + 1
            return SyncResult(users=[u for u in users if u.login], roles=counts)
        finally:
            self._close(conn)

    # -- probe -------------------------------------------------------------------------------
    def probe(self) -> list[dict[str, Any]]:
        s, checks = self.s, []

        def check(name: str, ok: bool, detail: str) -> bool:
            checks.append({"name": name, "ok": ok, "detail": detail})
            return ok

        transport = ("LDAPS" if all(u.lower().startswith("ldaps://") for u in s.urls)
                     else "StartTLS")
        # what is configured; whether the handshake works is the bind below
        check("transport", True, f"{transport}; the certificate will be "
              + ("verified" + (f" against {s.ca_file}" if s.ca_file else " (system CAs)")
                 if s.tls_verify else "NOT verified (tls_verify: false — test directories only)"))
        try:
            conn = self._service()
        except DirectoryError as exc:
            check("bind", False, str(exc))
            return checks
        try:
            check("bind", True, f"service account {s.bind_dn}")
            people = self._search(conn, s.users_at, s.people, [s.login_attr])
            check("users", bool(people), f"{len(people)} user(s) under {s.users_at}"
                  if people else f"no user under {s.users_at} matches {s.people}")
            if s.role_source.kind == "group":
                n = len(self._role_groups(conn))
                check("roles", n > 0, f"{n} group(s) under {s.groups_at} match {s.role_groups}")
            else:
                sample = self._search(conn, s.users_at, s.people, self._attributes())[:500]
                values = {r for e in sample for r in self._roles(conn, e)}
                what = (f"attribute {s.role_source.attribute}" if s.role_source.kind == "attribute"
                        else f"OUs under {s.ous_at}")
                check("roles", bool(values), f"{len(values)} distinct value(s) of {what} "
                      f"among {len(sample)} user(s)")
        except Exception as exc:
            check("search", False, f"{type(exc).__name__}: {exc}")
        finally:
            self._close(conn)
        return checks


def _ous(dn: str, base: str) -> list[RoleRef]:
    """The OUs a user sits in *below* ``base``, named relative to it: a user in
    ``ou=Team,ou=Finance,ou=People,dc=ex`` with base ``ou=People,dc=ex`` has the roles
    ``Finance`` and ``Finance/Team``."""
    parts = [p.strip() for p in dn.split(",")]
    base_n = _norm(base)
    depth = len(base_n.split(","))
    out = []
    for i in range(1, len(parts)):
        rest = _norm(",".join(parts[i:]))
        if not parts[i].lower().startswith("ou=") or not rest.endswith("," + base_n):
            continue
        below = parts[i:len(parts) - depth]
        names = [p.split("=", 1)[1] for p in reversed(below) if p.lower().startswith("ou=")]
        out.append(RoleRef("ou", rest, "/".join(names)))
    return sorted(out, key=lambda r: r.ref)


def _tls_connector(s: LdapSettings) -> Connector:
    def connect(dn: str, password: str):
        from ldap3 import FIRST, NONE, Connection, Server, ServerPool, Tls

        tls = Tls(validate=ssl.CERT_REQUIRED if s.tls_verify else ssl.CERT_NONE,
                  ca_certs_file=s.ca_file)
        servers = [Server(u, use_ssl=u.lower().startswith("ldaps://"), tls=tls,
                          connect_timeout=s.timeout_s, get_info=NONE) for u in s.urls]
        conn = Connection(ServerPool(servers, FIRST, active=1, exhaust=True), user=dn,
                          password=password, receive_timeout=s.timeout_s, read_only=True,
                          raise_exceptions=False, auto_referrals=False)
        conn.open()
        if s.start_tls and not conn.start_tls():
            raise DirectoryError("StartTLS failed")
        if not conn.bind():
            conn.unbind()
            return None
        return conn

    return connect
