"""An offline Active Directory for tests (``ldap3``'s MOCK_SYNC strategy).

    dc=acme,dc=test
      ou=Service   svc-gaia (the service account)
      ou=People
        ou=Finance  akaya  — GAIA-Finance; department Finance
        ou=HR       bdemir — Team-HR (which is in GAIA-HR: nested); department HR
                    cpasif — disabled (userAccountControl 514), GAIA-Finance
        ou=IT       dyilmaz — GAIA-Admins and GAIA-Finance; department IT
                    e*star — a login with filter characters, GAIA-Finance
      ou=Groups
        ou=Gaia     GAIA-Finance, GAIA-HR, GAIA-Admins, GAIA-Loop1 <-> GAIA-Loop2 (a cycle)
        ou=Teams    Team-HR (not a role: outside the role base, but traversed)

The mock supports simple binds against ``userPassword``, escaped filters and paged
searches — not AD's in-chain matching rule, so nesting is tested client-side here and the
``ad_in_chain`` strategy on a real AD (OWNER_VERIFICATION).
"""

from __future__ import annotations

from ldap3 import MOCK_SYNC, OFFLINE_AD_2012_R2, Connection, Server

BASE = "dc=acme,dc=test"
SVC = f"cn=svc-gaia,ou=Service,{BASE}"
SVC_PW = "service-Password-1"
PEOPLE = f"ou=People,{BASE}"
ROLE_BASE = f"ou=Gaia,ou=Groups,{BASE}"
PASSWORDS = {"akaya": "Ayse-Password-1", "bdemir": "Berk-Password-1",
             "cpasif": "Cem-Password-1", "dyilmaz": "Deniz-Password-1",
             "e*star": "Star-Password-1"}


def _user(cn: str, ou: str, login: str, dept: str, uac: int = 512) -> tuple[str, dict]:
    dn = f"cn={cn},ou={ou},{PEOPLE}"
    return dn, {"objectClass": ["top", "person", "user"], "cn": cn, "sAMAccountName": login,
                "userPrincipalName": f"{login}@acme.test", "displayName": cn,
                "mail": f"{login}@acme.test", "userAccountControl": uac,
                "department": [dept], "userPassword": PASSWORDS[login]}


USERS = dict([
    _user("Ayse Kaya", "Finance", "akaya", "Finance"),
    _user("Berk Demir", "HR", "bdemir", "HR"),
    _user("Cem Pasif", "HR", "cpasif", "HR", uac=514),
    _user("Deniz Yilmaz", "IT", "dyilmaz", "IT"),
    _user("Star User", "IT", "e*star", "IT"),
])
DN = {attrs["sAMAccountName"]: dn for dn, attrs in USERS.items()}


def _group(cn: str, ou: str, members: list[str], description: str = "") -> tuple[str, dict]:
    return f"cn={cn},ou={ou},ou=Groups,{BASE}", {
        "objectClass": ["top", "group"], "cn": cn, "member": members,
        "description": description or cn}


GROUPS = dict([
    _group("GAIA-Finance", "Gaia", [DN["akaya"], DN["cpasif"], DN["dyilmaz"], DN["e*star"]],
           "Finance analysts"),
    _group("Team-HR", "Teams", [DN["bdemir"]]),
    _group("GAIA-HR", "Gaia", [f"cn=Team-HR,ou=Teams,ou=Groups,{BASE}"], "HR"),
    _group("GAIA-Admins", "Gaia", [DN["dyilmaz"]], "Gaia administrators"),
    _group("GAIA-Loop1", "Gaia", [f"cn=GAIA-Loop2,ou=Gaia,ou=Groups,{BASE}", DN["akaya"]]),
    _group("GAIA-Loop2", "Gaia", [f"cn=GAIA-Loop1,ou=Gaia,ou=Groups,{BASE}"]),
])


def fake_server() -> Server:
    server = Server("dc1.acme.test", get_info=OFFLINE_AD_2012_R2)
    seed = Connection(server, user=SVC, password=SVC_PW, client_strategy=MOCK_SYNC)
    seed.strategy.add_entry(SVC, {"objectClass": ["top", "person"], "cn": "svc-gaia",
                                  "userPassword": SVC_PW})
    for dn, attrs in {**USERS, **GROUPS}.items():
        seed.strategy.add_entry(dn, attrs)
    return server


def connector(server: Server, calls: list | None = None):
    """What ``LdapDirectory`` needs: (dn, password) -> a bound connection or None."""

    def connect(dn: str, password: str):
        if calls is not None:
            calls.append(dn)
        conn = Connection(server, user=dn, password=password, client_strategy=MOCK_SYNC,
                          raise_exceptions=False)
        return conn if conn.bind() else None

    return connect


def settings(**role_source) -> dict:
    """Directory settings as an administrator would enter them (``auth.directory.settings``)."""
    return {"urls": ["ldaps://dc1.acme.test"], "flavor": "ad", "bind_dn": SVC, "base_dn": BASE,
            "user_base": PEOPLE, "user_filter": "(objectClass=user)",
            "role_source": {"kind": "group", "group_base": ROLE_BASE, **role_source}}
