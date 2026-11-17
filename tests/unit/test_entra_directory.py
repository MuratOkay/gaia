"""Phase 5A.2: Microsoft Entra ID as the identity provider, against an offline Microsoft."""

from __future__ import annotations

import time

import httpx
import pytest
from _fake_entra import CLIENT_ID, ISSUER, SECRET, TENANT, FakeMicrosoft, _key, settings
from pydantic import ValidationError

from gaia.auth.entra import EntraDirectory, EntraSettings, pkce_challenge
from gaia.auth.ldap import DirectoryError

VERIFIER = "v" * 43


def _entra(fake: FakeMicrosoft, secret: str = SECRET, **role_source) -> EntraDirectory:
    return EntraDirectory(EntraSettings.model_validate(settings(**role_source)), secret,
                          http=httpx.Client(transport=httpx.MockTransport(fake.handler)))


def _sign_in(fake: FakeMicrosoft, d: EntraDirectory, oid: str, nonce: str = "n-1"):
    url = d.authorize_url(state="s-1", nonce=nonce, verifier=VERIFIER)
    code, state = fake.authorize(url, oid)
    assert state == "s-1"
    return d.redeem(code, verifier=VERIFIER, nonce=nonce)


def test_a_sign_in_gives_the_person_and_their_groups():
    fake = FakeMicrosoft()
    d = _entra(fake)
    url = d.authorize_url(state="s", nonce="n", verifier=VERIFIER)
    assert "code_challenge=" + pkce_challenge(VERIFIER) in url and "nonce=n" in url
    ayse = _sign_in(fake, d, "a1")
    assert ayse.login == "ayse@corp.example" and ayse.external_id == "a1" and ayse.enabled
    assert [r.name for r in ayse.roles] == ["GAIA-Finance"]
    deniz = _sign_in(fake, d, "d4")
    # GAIA-Mail is a Microsoft 365 group — security-enabled, as the Entra admin center makes
    # them, yet not a security group (D79: it became a role on the owner's tenant); GAIA-List
    # is a distribution list; Team-HR has no GAIA- prefix
    assert [r.name for r in deniz.roles] == ["GAIA-Admins", "GAIA-Finance"]
    berk = _sign_in(fake, d, "b2")
    assert [r.name for r in berk.roles] == ["GAIA-HR"]  # nested, through Team-HR


def test_the_id_token_is_checked_thoroughly():
    for tamper, message in [
        ({"aud": "someone-else"}, "not valid"),
        ({"iss": "https://login.microsoftonline.com/other/v2.0"}, "not valid"),
        ({"exp": int(time.time()) - 3600}, "not valid"),
        ({"nonce": "another"}, "nonce"),
        ({"tid": "99999999-0000-4000-8000-000000000000"}, "another tenant"),
        ({"sign_with": (_key(), "key-1")}, "not valid"),  # a forged signature, known key id
        ({"sign_with": (_key(), "key-x")}, "does not publish"),
    ]:
        fake = FakeMicrosoft()
        fake.tamper = tamper
        with pytest.raises(DirectoryError, match=message):
            _sign_in(fake, _entra(fake), "a1")


def test_a_code_works_once_and_only_with_its_verifier():
    fake = FakeMicrosoft()
    d = _entra(fake)
    url = d.authorize_url(state="s", nonce="n", verifier=VERIFIER)
    code, _state = fake.authorize(url, "a1")
    with pytest.raises(DirectoryError, match="verifier"):
        d.redeem(code, verifier="w" * 43, nonce="n")
    code, _state = fake.authorize(url, "a1")
    d.redeem(code, verifier=VERIFIER, nonce="n")
    with pytest.raises(DirectoryError, match="already redeemed"):
        d.redeem(code, verifier=VERIFIER, nonce="n")


def test_rotated_keys_are_fetched_again():
    fake = FakeMicrosoft()
    d = _entra(fake)
    _sign_in(fake, d, "a1")
    fake.rotate()
    assert _sign_in(fake, d, "a1").login == "ayse@corp.example"
    assert fake.calls.count(f"GET /{TENANT}/discovery/v2.0/keys") == 2


def test_a_wrong_client_secret_says_so():
    fake = FakeMicrosoft()
    with pytest.raises(DirectoryError, match="Invalid client secret"):
        _sign_in(fake, _entra(fake, secret="wrong"), "a1")


def test_lookup_sees_the_directory_now():
    fake = FakeMicrosoft()
    d = _entra(fake)
    assert d.lookup("ayse@corp.example", "a1").enabled
    fake.users["a1"]["accountEnabled"] = False
    assert d.lookup("ayse@corp.example", "a1").enabled is False
    assert d.lookup("gone@corp.example", "zz") is None
    assert d.lookup("cem@corp.example").enabled is False  # by UPN when no id is known


def test_sync_reads_everyone_and_the_distinct_roles_across_pages():
    result = _entra(FakeMicrosoft()).sync()
    assert sorted(u.login.split("@")[0] for u in result.users) == ["ayse", "berk", "cem", "deniz"]
    counts = {r.name: n for r, n in result.roles.items()}
    assert counts == {"GAIA-Admins": 1, "GAIA-Finance": 3, "GAIA-HR": 1}


def test_roles_from_an_attribute():
    fake = FakeMicrosoft()
    d = _entra(fake, kind="attribute", attribute="department")
    assert [r.name for r in _sign_in(fake, d, "b2").roles] == ["HR"]
    counts = {r.name: n for r, n in d.sync().roles.items()}
    assert counts == {"Finance": 1, "HR": 2, "IT": 1}


def test_the_probe():
    fake = FakeMicrosoft()
    checks = {c["name"]: c for c in _entra(fake).probe()}
    assert all(c["ok"] for c in checks.values()), checks
    assert ISSUER in checks["discovery"]["detail"] and "3 group(s)" in checks["roles"]["detail"]
    # the probe names the roles and what it left out, and why (D79)
    assert "GAIA-Admins, GAIA-Finance, GAIA-HR" in checks["roles"]["detail"]
    assert ("left out: GAIA-List (not a security group), GAIA-Mail (a Microsoft 365 group)"
            in checks["roles"]["detail"])
    fake.graph_forbidden = True
    failed = [c for c in _entra(fake).probe() if not c["ok"]]
    assert [c["name"] for c in failed] == ["users"] and "User.Read.All" in failed[0]["detail"]
    bad = [c["name"] for c in _entra(FakeMicrosoft(), secret="x").probe() if not c["ok"]]
    assert bad == ["error"]


def test_settings_are_validated():
    ok = EntraSettings.model_validate(settings())
    assert ok.client_id == CLIENT_ID and ok.after_sign_in == "/"
    for change, message in [
        ({"redirect_uri": "http://gaia.corp.example/api/auth/oidc/callback"}, "https"),
        ({"redirect_uri": "https://gaia.corp.example/elsewhere"}, "oidc/callback"),
        ({"after_sign_in": "https://evil.example/"}, "path of this site"),
        ({"after_sign_in": "//evil.example/"}, "path of this site"),
        ({"authority": "http://login.microsoftonline.com"}, "https"),
        ({"role_source": {"kind": "attribute", "attribute": "salary"}}, "one of"),
        ({"role_source": {"kind": "group", "name_prefix": "x' or 1 eq 1"}}, "quote"),
    ]:
        with pytest.raises(ValidationError, match=message):
            EntraSettings.model_validate(settings() | change)
