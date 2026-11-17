"""Secrets Gaia itself checks: the local administrator's password, session tokens, CSRF
tokens and one-time setup codes (Phase 5A, D68).

* The password is hashed with **argon2id** (``argon2-cffi`` defaults: 3 passes, 64 MiB),
  never stored or logged in clear. Directory accounts have no password in Gaia at all.
* Tokens and codes are 256-bit random values; only their SHA-256 is stored. A hash of a
  random value that long needs no salt or stretching — nothing can be guessed from it.
* Comparisons are constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

__all__ = ["MIN_PASSWORD", "PasswordPolicyError", "check_policy", "hash_password",
           "needs_rehash", "new_token", "same_token", "token_hash", "verify_password"]

MIN_PASSWORD = 12
_HASHER = PasswordHasher()
# a hash of a password nobody has, so an unknown account costs the same time as a known one
_DUMMY = _HASHER.hash(secrets.token_urlsafe(24))


class PasswordPolicyError(ValueError):
    """The password is not acceptable (too short, or the login itself)."""


def check_policy(password: str, login: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise PasswordPolicyError(f"a password needs at least {MIN_PASSWORD} characters")
    if password.strip().lower() == login.strip().lower():
        raise PasswordPolicyError("the password cannot be the login")
    if len(set(password)) < 5:
        raise PasswordPolicyError("the password repeats too few characters")


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    """True only for the right password; a missing hash still costs one verification."""
    try:
        return _HASHER.verify(stored or _DUMMY, password) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _HASHER.check_needs_rehash(stored)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def same_token(token: str | None, stored_hash: str | None) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(token_hash(token), stored_hash)
