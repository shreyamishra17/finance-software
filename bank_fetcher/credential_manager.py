from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field
from typing import Optional

import keyring

KEYRING_SERVICE = "fifo_bank_fetcher"


class CredentialNotFoundError(RuntimeError):
    """Raised when a required credential entry is absent."""


@dataclass
class BankCredentials:
    username: str
    password: str = field(repr=False)
    totp_secret: Optional[str] = field(default=None, repr=False)

    def __str__(self) -> str:
        return f"BankCredentials(username={self.username!r}, password=REDACTED)"


# ------------------------------------------------------------------ #
# Fernet helpers — used by Smart Connect cloud credential storage
# ------------------------------------------------------------------ #

def _fernet_key() -> bytes:
    """
    Return the Fernet encryption key.
    Priority: FERNET_KEY env var → key stored in Firestore _meta/crypto.
    Generates and stores a new key on first use if neither is set.
    """
    from cryptography.fernet import Fernet
    env_key = os.environ.get("FERNET_KEY", "").strip()
    if env_key:
        return env_key.encode()
    # Fall back to key stored in Firestore
    try:
        from db import _get_db
        db = _get_db()
        doc = db.collection("_meta").document("crypto").get()
        if doc.exists:
            return doc.get("fernet_key").encode()
        key = Fernet.generate_key()
        db.collection("_meta").document("crypto").set({"fernet_key": key.decode()})
        return key
    except Exception:
        # If Firestore isn't available (e.g. unit tests), generate an ephemeral key
        return Fernet.generate_key()


def store_credentials_cloud(bank_key: str, username: str, password: str) -> None:
    """Encrypt credentials and persist to Firestore (used by Smart Connect)."""
    from cryptography.fernet import Fernet
    from db import _get_db
    f = Fernet(_fernet_key())
    encrypted_pw = f.encrypt(password.encode()).decode()
    _get_db().collection("credentials").document(bank_key).set({
        "username":           username,
        "encrypted_password": encrypted_pw,
    }, merge=True)


def get_credentials_cloud(bank_key: str) -> BankCredentials | None:
    """Retrieve and decrypt credentials from Firestore. Returns None if not stored."""
    from cryptography.fernet import Fernet
    from db import _get_db
    doc = _get_db().collection("credentials").document(bank_key).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    try:
        f = Fernet(_fernet_key())
        password = f.decrypt(data["encrypted_password"].encode()).decode()
        return BankCredentials(username=data["username"], password=password)
    except Exception:
        return None


# ------------------------------------------------------------------ #
# OS keychain helpers (local dev)
# ------------------------------------------------------------------ #

def setup_credentials(bank_key: str, fetcher_cfg: dict) -> None:
    """Interactive first-run credential storage into OS keychain."""
    svc = fetcher_cfg["keyring_service"]
    username = input(f"{fetcher_cfg['bank_display_name']} username: ").strip()
    password = getpass.getpass(f"{fetcher_cfg['bank_display_name']} password: ")
    keyring.set_password(svc, fetcher_cfg["keyring_username_key"], username)
    keyring.set_password(svc, fetcher_cfg["keyring_password_key"], password)
    totp_key = fetcher_cfg.get("keyring_totp_secret_key")
    if totp_key:
        use_totp = input("Use a TOTP authenticator app for MFA? [y/N]: ").strip().lower()
        if use_totp == "y":
            totp_secret = getpass.getpass("TOTP base32 secret: ")
            keyring.set_password(svc, totp_key, totp_secret)
    print(f"Credentials stored for '{bank_key}'.")


def get_credentials(fetcher_cfg: dict) -> BankCredentials:
    """
    Retrieve credentials. Tries OS keychain first (local dev),
    then falls back to Firestore (Cloud Run / Smart Connect).
    """
    svc = fetcher_cfg["keyring_service"]
    bank_key = fetcher_cfg["bank_key"]

    # Try OS keychain (works locally)
    try:
        username = keyring.get_password(svc, fetcher_cfg["keyring_username_key"])
        password = keyring.get_password(svc, fetcher_cfg["keyring_password_key"])
        if username and password:
            totp_key = fetcher_cfg.get("keyring_totp_secret_key")
            totp_secret = keyring.get_password(svc, totp_key) if totp_key else None
            return BankCredentials(username=username, password=password, totp_secret=totp_secret)
    except Exception:
        pass

    # Fall back to Firestore (Cloud Run)
    cloud_creds = get_credentials_cloud(bank_key)
    if cloud_creds:
        return cloud_creds

    raise CredentialNotFoundError(
        f"No credentials found for '{bank_key}'. "
        f"Use Smart Connect to save credentials, or locally run: "
        f"python -m bank_fetcher setup --bank {bank_key}"
    )


def delete_credentials(bank_key: str, fetcher_cfg: dict) -> None:
    """Remove keychain entries for a bank."""
    svc = fetcher_cfg["keyring_service"]
    for key_field in ("keyring_username_key", "keyring_password_key", "keyring_totp_secret_key"):
        label = fetcher_cfg.get(key_field)
        if label:
            try:
                keyring.delete_password(svc, label)
            except Exception:
                pass
    print(f"Credentials deleted for '{bank_key}'.")


def generate_totp(totp_secret: str) -> str:
    """Return current 6-digit TOTP code."""
    import pyotp
    return pyotp.TOTP(totp_secret).now()
