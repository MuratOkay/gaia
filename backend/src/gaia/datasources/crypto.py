"""Encryption of data-source credentials at rest (Phase 4F, P4-6).

AES-256-GCM in the application; the key never enters the database. The master key
is ``GAIA_MASTER_KEY`` (32 random bytes, URL-safe base64 — ``make master-key`` prints
one). Every ciphertext records the id of the key that made it (the first 12 hex
digits of the key's SHA-256, not the key), and the **data source's id is the
associated data**: a ciphertext copied onto another row does not decrypt.

Rotation: put the new key in ``GAIA_MASTER_KEY`` and the old one in
``GAIA_MASTER_KEY_PREVIOUS`` (comma-separated if several), run ``make datasource
ARGS=rotate-key``, then remove the old key. Decryption picks the key by its id.

Nothing here logs, and no error carries a secret, a key or a ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["KEY_ENV", "PREVIOUS_ENV", "CredentialError", "decrypt", "encrypt", "key_id",
           "new_key"]

KEY_ENV = "GAIA_MASTER_KEY"
PREVIOUS_ENV = "GAIA_MASTER_KEY_PREVIOUS"
_NONCE = 12


class CredentialError(RuntimeError):
    """A credential cannot be encrypted or decrypted (no key, wrong key, tampered)."""


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _decode(text: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(text.strip() + "=" * (-len(text.strip()) % 4))
    except (ValueError, TypeError):
        raise CredentialError("a master key is not valid base64") from None
    if len(raw) != 32:
        raise CredentialError("a master key must be 32 bytes (make master-key prints one)")
    return raw


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:12]


def _keys() -> tuple[bytes, dict[str, bytes]]:
    from gaia.db.engine import _ensure_dotenv

    _ensure_dotenv()
    current = os.environ.get(KEY_ENV, "").strip()
    if not current:
        raise CredentialError(f"{KEY_ENV} is not set: add one to .env (make master-key)")
    key = _decode(current)
    keys = {key_id(key): key}
    for text in os.environ.get(PREVIOUS_ENV, "").split(","):
        if text.strip():
            old = _decode(text)
            keys.setdefault(key_id(old), old)
    return key, keys


def encrypt(secret: str, data_source_id: uuid.UUID) -> tuple[bytes, str]:
    """(nonce + ciphertext, key id) with the current master key."""
    key, _keys_by_id = _keys()
    nonce = os.urandom(_NONCE)
    blob = nonce + AESGCM(key).encrypt(nonce, secret.encode("utf-8"), data_source_id.bytes)
    return blob, key_id(key)


def decrypt(blob: bytes, kid: str, data_source_id: uuid.UUID) -> str:
    _key, keys = _keys()
    key = keys.get(kid)
    if key is None:
        raise CredentialError(
            f"the credential was encrypted with key {kid}, which is neither {KEY_ENV} nor in "
            f"{PREVIOUS_ENV}")
    try:
        plain = AESGCM(key).decrypt(bytes(blob[:_NONCE]), bytes(blob[_NONCE:]),
                                    data_source_id.bytes)
    except InvalidTag:
        raise CredentialError("the credential does not decrypt (wrong key, another data "
                              "source's credential, or altered)") from None
    return plain.decode("utf-8")
