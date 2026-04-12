"""
core/encryption.py — Fernet-based symmetric encryption for session artifacts.

The key is auto-generated on first use and stored in macOS Keychain via keyring.
It is never written to disk or committed to version control.
"""
from __future__ import annotations

import keyring
from cryptography.fernet import Fernet

_KEYRING_SERVICE = "fifo_encryption"
_KEYRING_KEY = "fernet_key"


def _get_or_create_key() -> bytes:
    key = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
    if not key:
        key = Fernet.generate_key().decode()
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, key)
    return key.encode()


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns a URL-safe base64 Fernet token."""
    return Fernet(_get_or_create_key()).encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet token back to plaintext.
    Raises cryptography.fernet.InvalidToken if tampered or wrong key."""
    return Fernet(_get_or_create_key()).decrypt(token.encode()).decode()
