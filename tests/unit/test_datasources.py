"""Phase 4F without a database: credential encryption and the data-source spec."""

from __future__ import annotations

import uuid

import pytest

from gaia.datasources.crypto import CredentialError, decrypt, encrypt, key_id, new_key
from gaia.datasources.service import DataSourceSpec


@pytest.fixture()
def master(monkeypatch):
    key = new_key()
    monkeypatch.setenv("GAIA_MASTER_KEY", key)
    monkeypatch.delenv("GAIA_MASTER_KEY_PREVIOUS", raising=False)
    return key


def test_round_trip_and_binding_to_the_data_source(master):
    ds = uuid.uuid4()
    blob, kid = encrypt("s3cr3t; with @ special", ds)
    assert b"s3cr3t" not in blob and len(blob) > 12
    assert decrypt(blob, kid, ds) == "s3cr3t; with @ special"
    with pytest.raises(CredentialError, match="does not decrypt"):
        decrypt(blob, kid, uuid.uuid4())  # copied onto another data source's row
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(CredentialError):
        decrypt(tampered, kid, ds)
    a, _ = encrypt("same", ds)
    b, _ = encrypt("same", ds)
    assert a != b  # a fresh nonce every time


def test_keys_rotation_and_errors(master, monkeypatch):
    ds = uuid.uuid4()
    old_blob, old_kid = encrypt("pw", ds)
    newer = new_key()
    monkeypatch.setenv("GAIA_MASTER_KEY", newer)
    with pytest.raises(CredentialError, match="neither GAIA_MASTER_KEY nor"):
        decrypt(old_blob, old_kid, ds)
    monkeypatch.setenv("GAIA_MASTER_KEY_PREVIOUS", master)
    assert decrypt(old_blob, old_kid, ds) == "pw"  # during the rotation
    blob, kid = encrypt("pw", ds)
    assert kid != old_kid and kid == key_id(__import__("base64").urlsafe_b64decode(newer))
    monkeypatch.setenv("GAIA_MASTER_KEY", "")
    with pytest.raises(CredentialError, match="not set"):
        encrypt("pw", ds)
    monkeypatch.setenv("GAIA_MASTER_KEY", "dG9vIHNob3J0")
    with pytest.raises(CredentialError, match="32 bytes") as info:
        encrypt("pw", ds)
    assert "dG9vIHNob3J0" not in str(info.value)  # no key in a message


def test_spec_rules():
    spec = DataSourceSpec(name="sales_pg", dialect="postgresql", host="10.0.0.5", port=5432,
                          database="sales", username="gaia_ro", options={"sslmode": "require"},
                          limits={"statement_timeout_s": 20})
    assert spec.dialect == "postgres"
    doc = spec.connection()
    assert "password" not in doc and doc["limits"] == {"statement_timeout_s": 20}
    for bad in ("Sales", "9sales", "x", "sales-pg", "sales pg"):
        with pytest.raises(ValueError):
            DataSourceSpec(name=bad, dialect="postgres", host="h", port=1, database="d",
                           username="u")
    with pytest.raises(ValueError):
        DataSourceSpec(name="ok_name", dialect="oracle", host="h", port=1, database="d",
                       username="u")
    with pytest.raises(ValueError):  # a limit that is not a limit
        DataSourceSpec(name="ok_name", dialect="postgres", host="h", port=1, database="d",
                       username="u", limits={"statement_timeout_s": "soon"}).connection()


def test_the_admin_request_never_prints_its_password():
    from gaia.api.admin import NewDataSource

    body = NewDataSource(name="sales_pg", dialect="postgres", host="h", port=5432,
                         database="d", username="u", password="hunter2")  # noqa: S106
    assert "hunter2" not in repr(body) and "hunter2" not in str(body)
