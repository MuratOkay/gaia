"""Phase 5A: Active Directory / LDAP as the identity provider, on an offline directory."""

from __future__ import annotations

import pytest
from _fake_directory import (
    DN,
    PASSWORDS,
    PEOPLE,
    SVC_PW,
    connector,
    fake_server,
    settings,
)
from pydantic import ValidationError

from gaia.auth.ldap import DirectoryError, LdapDirectory, LdapSettings


def _directory(calls=None, **role_source) -> LdapDirectory:
    return LdapDirectory(LdapSettings.model_validate(settings(**role_source)), SVC_PW,
                         connect=connector(fake_server(), calls))


def _names(user) -> list[str]:
    return [r.name for r in user.roles]


def test_sign_in_with_the_directory_password_and_group_roles():
    d = _directory()
    user = d.authenticate("akaya", PASSWORDS["akaya"])
    assert user is not None and user.enabled and user.login == "akaya"
    assert user.display_name == "Ayse Kaya" and user.email == "akaya@acme.test"
    assert _names(user) == ["GAIA-Finance", "GAIA-Loop1", "GAIA-Loop2"]  # a cycle ends
    # the same person by UPN and by DOMAIN\login
    assert d.authenticate("akaya@acme.test", PASSWORDS["akaya"]).login == "akaya"
    assert d.authenticate("ACME\\akaya", PASSWORDS["akaya"]).login == "akaya"


def test_wrong_unknown_or_empty_all_say_the_same_thing():
    calls: list[str] = []
    d = _directory(calls)
    assert d.authenticate("akaya", "wrong-password") is None
    assert d.authenticate("nobody", PASSWORDS["akaya"]) is None
    # an empty password is an anonymous bind on AD: refused before the directory is asked
    calls.clear()
    assert d.authenticate("akaya", "") is None and calls == []


def test_filter_characters_in_a_login_are_escaped():
    d = _directory()
    assert d.authenticate("*", PASSWORDS["akaya"]) is None  # no wildcard sign-in
    assert d.authenticate("akaya)(sAMAccountName=*", PASSWORDS["akaya"]) is None
    star = d.authenticate("e*star", PASSWORDS["e*star"])
    assert star is not None and star.login == "e*star"


def test_nested_groups_are_followed_through_groups_outside_the_role_base():
    d = _directory()
    berk = d.authenticate("bdemir", PASSWORDS["bdemir"])
    assert _names(berk) == ["GAIA-HR"]  # via Team-HR, which is not a role itself
    flat = _directory(nested=False).authenticate("bdemir", PASSWORDS["bdemir"])
    assert _names(flat) == []


def test_roles_from_an_attribute_or_from_the_ou():
    by_dept = LdapDirectory(
        LdapSettings.model_validate(settings() | {"role_source": {"kind": "attribute",
                                                                  "attribute": "department"}}),
        SVC_PW, connect=connector(fake_server()))
    assert _names(by_dept.authenticate("bdemir", PASSWORDS["bdemir"])) == ["HR"]
    by_ou = LdapDirectory(
        LdapSettings.model_validate(settings() | {"role_source": {"kind": "ou",
                                                                  "ou_base": PEOPLE}}),
        SVC_PW, connect=connector(fake_server()))
    assert _names(by_ou.authenticate("akaya", PASSWORDS["akaya"])) == ["Finance"]
    from gaia.auth.ldap import _ous

    deep = _ous("cn=x,ou=Team,ou=Finance,ou=People,dc=ex", "OU=People, DC=ex")
    assert [(r.name, r.ref) for r in deep] == [
        ("Finance", "ou=finance,ou=people,dc=ex"), ("Finance/Team", "ou=team,ou=finance,ou=people,dc=ex")]


def test_a_disabled_account_is_reported_disabled():
    # the mock accepts its bind; a real AD refuses it — either way Gaia does not let it in
    user = _directory().lookup("cpasif")
    assert user is not None and user.enabled is False


def test_lookup_needs_no_password_and_sees_the_directory_now():
    d = _directory()
    assert _names(d.lookup("dyilmaz")) == ["GAIA-Admins", "GAIA-Finance"]
    assert d.lookup("gone-person") is None


def test_sync_reads_every_user_and_the_distinct_roles():
    result = _directory().sync()
    logins = sorted(u.login for u in result.users)
    assert logins == ["akaya", "bdemir", "cpasif", "dyilmaz", "e*star"]
    counts = {r.name: n for r, n in result.roles.items()}
    assert counts["GAIA-Finance"] == 4 and counts["GAIA-HR"] == 1 and counts["GAIA-Admins"] == 1
    by_login = {u.login: u for u in result.users}
    assert _names(by_login["bdemir"]) == ["GAIA-HR"]  # nesting resolved in memory as well
    assert by_login["cpasif"].enabled is False


def test_the_probe_lists_what_works():
    checks = {c["name"]: c for c in _directory().probe()}
    assert all(c["ok"] for c in checks.values()), checks
    assert "will be verified" in checks["transport"]["detail"]
    assert "5 user(s)" in checks["users"]["detail"]
    assert "group(s)" in checks["roles"]["detail"]
    bad = LdapDirectory(LdapSettings.model_validate(settings()), "wrong",
                        connect=connector(fake_server())).probe()
    assert [c["name"] for c in bad if not c["ok"]] == ["bind"]


def test_a_wrong_service_password_is_a_directory_error_not_a_wrong_user_password():
    d = LdapDirectory(LdapSettings.model_validate(settings()), "wrong",
                      connect=connector(fake_server()))
    with pytest.raises(DirectoryError, match="service account"):
        d.authenticate("akaya", PASSWORDS["akaya"])


def test_clear_text_ldap_is_refused():
    with pytest.raises(ValidationError, match="clear-text"):
        LdapSettings.model_validate(settings() | {"urls": ["ldap://dc1.acme.test"]})
    ok = LdapSettings.model_validate(settings() | {"urls": ["ldap://dc1.acme.test"],
                                                   "start_tls": True})
    assert ok.start_tls
    with pytest.raises(ValidationError, match="attribute"):
        LdapSettings.model_validate(settings() | {"role_source": {"kind": "attribute"}})


def test_dn_of_a_user_is_kept():
    user = _directory().authenticate("akaya", PASSWORDS["akaya"])
    assert user.dn.lower() == DN["akaya"].lower() and user.external_id
