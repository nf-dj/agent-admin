"""Symmetric encryption helpers for at-rest secrets (API keys, etc.).

Uses Fernet (AES-128-CBC + HMAC) with a key deterministically derived from
``settings.secret_key``. This means:

* Rotating ``ADMIN_SECRET_KEY`` invalidates all stored ciphertexts.
  Plan accordingly — if you rotate, you'll need a re-encryption migration
  (or just have users re-enter their keys).
* The key never appears in plaintext on disk.

For v1 this is "good enough" — not envelope encryption, no KMS, no key
versioning. Upgrade path is straightforward if needed: add a ``key_version``
column and a key registry.
"""
from __future__ import annotations

import base64
import functools
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


@functools.lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Derive a stable Fernet key from the app secret. Cached so we only
    hash once per process."""
    # Fernet keys are URL-safe base64 of 32 raw bytes.
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """Return a base64-ish ASCII ciphertext safe to store in TEXT columns."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Inverse of ``encrypt``. Raises ``InvalidToken`` if the secret rotated
    or the ciphertext was tampered with."""
    return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def safe_decrypt(ciphertext: str) -> str | None:
    """Like ``decrypt`` but returns ``None`` on failure instead of raising.

    Useful for endpoints that should degrade gracefully (e.g. show "Not set"
    when the secret has been rotated).
    """
    try:
        return decrypt(ciphertext)
    except InvalidToken:
        return None


def make_preview(plaintext: str) -> str:
    """Build a short, safe hint for the UI: first 4 + ``\u2026`` + last 4 chars.

    For very short strings, falls back to ``*****`` (we don't want to reveal
    more than a tiny fraction of the secret).
    """
    if len(plaintext) <= 10:
        return "*" * min(8, len(plaintext))
    return f"{plaintext[:4]}\u2026{plaintext[-4:]}"
