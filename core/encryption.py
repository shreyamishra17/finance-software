"""
core/encryption.py — Fernet-based symmetric encryption for session artifacts.

The key is auto-generated on first use and stored in macOS Keychain via keyring.
It is never written to disk or committed to version control.

The key is cached in memory after first load so that background threads (APScheduler)
can decrypt sessions without hitting the Keychain, which macOS blocks from non-UI
contexts with -25320 (errSecInteractionNotAllowed).
"""
from __future__ import annotations

import keyring
from cryptography.fernet import Fernet

_KEYRING_SERVICE = "fifo_encryption"
_KEYRING_KEY = "fernet_key"

# In-memory cache — populated on first successful Keychain read.
# Survives for the lifetime of the process, allowing background threads to
# decrypt sessions without a live UI session.
_cached_key: bytes | None = None


def _get_or_create_key() -> bytes:
    global _cached_key
    if _cached_key is not None:
        return _cached_key
    key = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
    if not key:
        key = Fernet.generate_key().decode()
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, key)
    _cached_key = key.encode()
    return _cached_key


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns a URL-safe base64 Fernet token."""
    return Fernet(_get_or_create_key()).encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet token back to plaintext.
    Raises cryptography.fernet.InvalidToken if tampered or wrong key."""
    return Fernet(_get_or_create_key()).decrypt(token.encode()).decode()
