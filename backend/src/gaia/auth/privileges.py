"""From grants to what a person may do (D68, D70).

Gaia roles: ``user`` (asks), ``sme`` (reviews the data sources it is granted for, asks),
``admin`` (administers, asks, never reviews), ``auditor`` (reads the audit reports).
One permission: ``pii_processor`` (sees personal data unmasked, D69; applied in 5C).

**Separation of duties.** An administrator cannot review (owner, P5-6) and cannot audit
their own work (D70). When grants would make someone both, the administrator role stays
and the other is *not applied*: the conflict is kept with the session, shown to
administrators and in the auditor's report — never silently resolved.

The local account (``gaia-admin``) is an administrator and nothing else, whatever the
grants table says: it exists for setup and break-glass only (D68).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["SIGN_IN_ROLES", "Effective", "Held", "effective"]

SIGN_IN_ROLES = ("user", "sme", "admin", "auditor")  # at least one is needed to sign in
_ROLE_ORDER = {r: i for i, r in enumerate(SIGN_IN_ROLES)}


@dataclass(frozen=True)
class Held:
    """One grant that applies to a person: directly, or through a directory role."""

    privilege: str
    data_source_id: str | None = None
    via: str = "account"  # "account", or the directory role's name


@dataclass
class Effective:
    roles: set[str] = field(default_factory=set)
    sme_sources: set[str] = field(default_factory=set)  # data source ids (str)
    pii_processor: bool = False
    conflicts: list[str] = field(default_factory=list)
    via: dict[str, list[str]] = field(default_factory=dict)  # privilege -> where from

    def has(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))

    @property
    def can_sign_in(self) -> bool:
        return bool(self.roles)

    def reviews(self, data_source_id: str) -> bool:
        return "sme" in self.roles and str(data_source_id) in self.sme_sources

    def to_json(self) -> dict[str, Any]:
        return {"roles": sorted(self.roles, key=_ROLE_ORDER.get),
                "sme_sources": sorted(self.sme_sources), "pii_processor": self.pii_processor,
                "conflicts": self.conflicts, "via": self.via}

    @classmethod
    def from_json(cls, doc: dict[str, Any] | None) -> Effective:
        doc = doc or {}
        return cls(roles=set(doc.get("roles") or []),
                   sme_sources=set(doc.get("sme_sources") or []),
                   pii_processor=bool(doc.get("pii_processor")),
                   conflicts=list(doc.get("conflicts") or []),
                   via=dict(doc.get("via") or {}))


def effective(held: Iterable[Held], *, local: bool = False) -> Effective:
    if local:
        return Effective(roles={"admin"}, via={"admin": ["the local account"]})
    out = Effective()
    for h in held:
        out.via.setdefault(h.privilege, []).append(h.via)
        if h.privilege == "pii_processor":
            out.pii_processor = True
        elif h.privilege == "sme":
            if h.data_source_id:
                out.roles.add("sme")
                out.sme_sources.add(str(h.data_source_id))
        elif h.privilege in SIGN_IN_ROLES:
            out.roles.add(h.privilege)
    if "admin" in out.roles:
        if "sme" in out.roles:
            out.roles.discard("sme")
            out.sme_sources.clear()
            out.conflicts.append("admin and sme: the sme role is not applied — an "
                                 "administrator cannot review (D68)")
        if "auditor" in out.roles:
            out.roles.discard("auditor")
            out.conflicts.append("admin and auditor: the auditor role is not applied — an "
                                 "administrator cannot audit their own work (D70)")
    out.via = {k: sorted(set(v)) for k, v in sorted(out.via.items())}
    return out
