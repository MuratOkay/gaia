"""``make share`` (D79): verification outputs without personal data, still triageable."""

from __future__ import annotations

from gaia.tools.redact import Redactor, main

OUTPUT = """cd /home/jdoe/g120s/gaia && /home/jdoe/g120s/gaia/gaia-env/bin/pytest -q
  corp/gaia-test1@contoso123.onmicrosoft.com admin
  corp/jane.doe_example.com#ext#@contoso123.onmicrosoft.com no Gaia role
      [PASS] discovery tenant issuer https://login.microsoftonline.com/0f1e2d3c-4b5a-4c6d-8e7f-112233445566/v2.0
      [PASS] credentials the app aabbccdd-1122-4334-8556-778899aabbcc obtained a Graph token
  user  role acme/GAIA-Finance  by cli:jdoe  48848427-7e34-4e2a-8a35-89922ae4f434
one-time setup code for gaia-admin, valid 30 min:
  ZehF6OCxDPSG1XSZ7TqMjIbehx7YpavDziPoMlmndoc
audit chain: intact — 146 event(s); head fbec3255d800c36e95c7c47da4550690741d138ba422a629b9aac69a26a92f65
  acme/akaya  user, sme + PII (sme: demo_pg)  sql1 at 192.168.1.20, target 127.0.0.1:5434
issuer of the tenant 0F1E2D3C-4B5A-4C6D-8E7F-112233445566 again; Jane Doe asked
"""


def test_personal_data_goes_and_the_output_can_still_be_read():
    r = Redactor(also=["Jane Doe"], os_user="jdoe")
    out = r.text(OUTPUT)
    for gone in ("jdoe", "contoso123", "jane.doe", "#ext#", "0f1e2d3c", "aabbccdd",
                 "48848427", "ZehF6OCx", "192.168.1.20", "Jane Doe"):
        assert gone.lower() not in out.lower(), gone
    # consistent: the same value is the same token, so the lines can still be compared
    assert out.count("<id-1>") == 2  # the tenant id, in either case
    assert "corp/<email-1> admin" in out and "corp/<email-2> no Gaia role" in out
    assert "/home/<user>/g120s" in out and "cli:<user>" in out
    # kept: what triage needs
    for kept in ("fbec3255d800c36e95c7c47da4550690741d138ba422a629b9aac69a26a92f65",
                 "acme/akaya", "demo_pg", "127.0.0.1:5434", "GAIA-Finance", "146 event(s)"):
        assert kept in out, kept
    assert r.key["ZehF6OCxDPSG1XSZ7TqMjIbehx7YpavDziPoMlmndoc"].startswith("<secret-")


def test_the_command_writes_copies_and_keeps_the_key_apart(tmp_path):
    f = tmp_path / "27_x.out"
    f.write_text(OUTPUT, encoding="utf-8")
    assert main([str(f), "--out", str(tmp_path / "share"), "--also", "Jane Doe"]) == 0
    shared = (tmp_path / "share" / "27_x.out").read_text(encoding="utf-8")
    assert "contoso123" not in shared and "<email-1>" in shared
    key = (tmp_path / "share-key.json").read_text(encoding="utf-8")
    assert "contoso123" in key  # with the owner, never in share/
    assert not list((tmp_path / "share").glob("*.json"))
    assert f.read_text(encoding="utf-8") == OUTPUT  # the original untouched
