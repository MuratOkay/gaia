"""``make share`` (D79): verification outputs without personal data, still triageable."""

from __future__ import annotations

from gaia.tools.redact import Redactor, main, redact_tree

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


def test_the_tree_mode_redacts_documents_and_leaves_code_alone(tmp_path):
    """``make export``: a copy is cleaned in place; source is what makes it run."""
    root = tmp_path / "copy"
    (root / "docs").mkdir(parents=True)
    (root / "backend" / "src").mkdir(parents=True)
    (root / "__pycache__").mkdir()
    doc = root / "docs" / "note.md"
    doc.write_text("mail jane@initech-mail.com, id 0f1e2d3c-4b5a-4c6d-8e7f-112233445566, "
                   "host 192.168.1.20, by Acme Ltd, shape aaaa.aaaa99@aaaa.aaa\n",
                   encoding="utf-8")
    code = root / "backend" / "src" / "mod.py"
    code.write_text("ADDRESS = 'jane@real.example.net'  # Acme Ltd\n", encoding="utf-8")
    prompt = root / "backend" / "src" / "prompts" / "reason.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("a shape like aaaa.aaaa99@aaaa.aaa is an address\n", encoding="utf-8")
    lock = root / "frontend" / "pnpm-lock.yaml"
    lock.parent.mkdir()
    lock.write_text("resolution: {integrity: sha512-6OzddxPio9UiWTCemp4N8cYLV2ZN1n==}\n",
                    encoding="utf-8")
    cache = root / "__pycache__" / "mod.pyc"
    cache.write_bytes(b"\x00jane@real.example.net")
    key = tmp_path / "key.json"

    assert main(["--tree", str(root), "--key", str(key), "--also", "Acme Ltd"]) == 0

    text = doc.read_text(encoding="utf-8")
    for gone in ("jane@initech-mail.com", "Acme Ltd"):
        assert gone not in text, gone
    assert "<email-1>" in text and "<name-1>" in text
    # a document keeps what is an id, an internal address or a masked shape
    for kept in ("0f1e2d3c", "192.168.1.20", "aaaa.aaaa99@aaaa.aaa"):
        assert kept in text, kept
    assert code.read_text(encoding="utf-8") == "ADDRESS = 'jane@real.example.net'  # <name-1>\n"
    assert prompt.read_text(encoding="utf-8") == "a shape like aaaa.aaaa99@aaaa.aaa is an address\n"
    assert lock.read_text(encoding="utf-8").endswith("1n==}\n")  # a lock file is not a document
    assert cache.read_bytes() == b"\x00jane@real.example.net"
    assert "jane@initech-mail.com" in key.read_text(encoding="utf-8")  # owner's key
    assert not (root / "key.json").exists()  # never inside the copy


def test_the_os_user_is_replaced_in_any_case(tmp_path):
    """``make export``: ``Mira`` in a document is the login ``mira`` the scan flags."""
    root = tmp_path / "copy"
    root.mkdir()
    page = root / "README.md"
    page.write_text("Copyright (c) 2026 Mira Chen.\n", encoding="utf-8")

    read, changed = redact_tree(root, Redactor(os_user="mira"))

    assert (read, changed) == (1, 1)
    assert page.read_text(encoding="utf-8") == "Copyright (c) 2026 <user> Chen.\n"


def test_a_documents_credentials_become_tokens(tmp_path):
    """``make export``: what the scan would refuse is a placeholder, not a leak."""
    # Built at run time: no literal in this file reads as a credential.
    password, uri_password = "Zx9" + "RealPassw0rd", "S3cret" + "Pass"
    bearer = "abcdefghijklmnopqrstuvwxyz" + "012345"
    github = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    sql_password = "Hx8" + "SqlPassw0rd"
    root = tmp_path / "copy"
    root.mkdir()
    page = root / "run.md"
    page.write_text(
        'password = "' + password + '"\n'
        "DATABASE_URL=postgresql://gaia:" + uri_password + "@127.0.0.1:5433/gaia\n"
        "Authorization: Bearer " + bearer + "\n"
        'API_KEY = "' + github + '"\n'
        "CREATE ROLE app LOGIN PASSWORD '" + sql_password + "'\n"
        'password = "<password>"\n', encoding="utf-8")

    redact_tree(root, Redactor())

    text = page.read_text(encoding="utf-8")
    for gone in (password, uri_password, bearer, github, sql_password):
        assert gone not in text, gone
    assert text.count("<secret-") == 5  # one token per credential, quotes kept
    assert "postgresql://gaia:<secret-" in text and "@127.0.0.1:5433/gaia" in text
    assert "Bearer <secret-" in text and "PASSWORD '<secret-" in text
    assert 'password = "<password>"' in text  # a placeholder names a credential


def test_a_private_key_in_a_document_is_replaced(tmp_path):
    begin, end = "-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"
    root = tmp_path / "copy"
    root.mkdir()
    note = root / "note.txt"
    note.write_text(f"before\n{begin}\nQUJD\n{end}\nafter\n", encoding="utf-8")

    redact_tree(root, Redactor())

    out = note.read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in out and "QUJD" not in out
    assert out == "before\n<private-key-1>\nafter\n"


def test_a_document_keeps_a_run_id_and_a_private_host(tmp_path):
    """A tenant-scoped id and a routable host are traces; a run id is not."""
    root = tmp_path / "copy"
    root.mkdir()
    page = root / "README.md"
    page.write_text(
        "issuer https://login.microsoftonline.com/0f1e2d3c-4b5a-4c6d-8e7f-112233445566/v2.0\n"
        "host 93.184.216.34 and 10.1.2.3\n"
        "run 0f1e2d3c-4b5a-4c6d-8e7f-112233445566\n", encoding="utf-8")

    redact_tree(root, Redactor())

    out = page.read_text(encoding="utf-8")
    assert "login.microsoftonline.com/<id-1>" in out
    assert "<ip-1>" in out and "93.184.216.34" not in out
    assert "10.1.2.3" in out  # a private address is infrastructure
    assert "run 0f1e2d3c-4b5a-4c6d-8e7f-112233445566" in out  # not tenant-scoped


def test_the_tree_mode_reads_words_from_a_file_and_check_writes_nothing(tmp_path):
    root = tmp_path / "copy"
    root.mkdir()
    page = root / "README.md"
    page.write_text("Acme Ltd ships gaia.\n", encoding="utf-8")
    words = tmp_path / "also.txt"
    words.write_text("# a company no pattern can know\nAcme Ltd\n", encoding="utf-8")

    assert main(["--tree", str(root), "--also-file", str(words), "--check"]) == 0
    assert page.read_text(encoding="utf-8") == "Acme Ltd ships gaia.\n"

    assert main(["--tree", str(root), "--also-file", str(words), "--key", str(tmp_path / "k.json")]) == 0
    assert page.read_text(encoding="utf-8").startswith("<name-1> ships")

