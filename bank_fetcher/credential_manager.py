from __future__ import annotations

import getpass
from dataclasses import dataclass, field
from typing import Optional

import keyring

KEYRING_SERVICE = "fifo_bank_fetcher"


class CredentialNotFoundError(RuntimeError):
    """Raised when a required keychain entry is absent."""


@dataclass
class BankCredentials:
    username: str
    password: str = field(repr=False)
    totp_secret: Optional[str] = field(default=None, repr=False)

    def __str__(self) -> str:
        return f"BankCredentials(username={self.username!r}, password=REDACTED)"


def setup_credentials(bank_key: str, fetcher_cfg: dict) -> None:
    """Interactive first-run credential storage into OS keychain."""
    svc = fetcher_cfg["keyring_service"]

    username = input(f"{fetcher_cfg['bank_display_name']} username (your online banking ID): ").strip()
    password = getpass.getpass(f"{fetcher_cfg['bank_display_name']} password: ")

    keyring.set_password(svc, fetcher_cfg["keyring_username_key"], username)
    keyring.set_password(svc, fetcher_cfg["keyring_password_key"], password)

    totp_key = fetcher_cfg.get("keyring_totp_secret_key")
    if totp_key:
        use_totp = input("Do you use a TOTP authenticator app for MFA? [y/N]: ").strip().lower()
        if use_totp == "y":
            totp_secret = getpass.getpass("TOTP base32 secret (from authenticator app QR code): ")
            keyring.set_password(svc, totp_key, totp_secret)
            print("TOTP secret stored.")

    print(f"Credentials stored in OS keychain for '{bank_key}'.")


def get_credentials(fetcher_cfg: dict) -> BankCredentials:
    """Retrieve credentials from OS keychain. Raises CredentialNotFoundError if missing."""
    svc = fetcher_cfg["keyring_service"]
    bank_key = fetcher_cfg["bank_key"]

    username = keyring.get_password(svc, fetcher_cfg["keyring_username_key"])
    password = keyring.get_password(svc, fetcher_cfg["keyring_password_key"])

    if not username or not password:
        raise CredentialNotFoundError(
            f"No credentials found for '{bank_key}'. "
            f"Run: python -m bank_fetcher setup --bank {bank_key}"
        )

    totp_key = fetcher_cfg.get("keyring_totp_secret_key")
    totp_secret = keyring.get_password(svc, totp_key) if totp_key else None

    return BankCredentials(username=username, password=password, totp_secret=totp_secret)


def delete_credentials(bank_key: str, fetcher_cfg: dict) -> None:
    """Remove all keychain entries for a bank (for credential rotation)."""
    svc = fetcher_cfg["keyring_service"]

    for key_field in ("keyring_username_key", "keyring_password_key", "keyring_totp_secret_key"):
        label = fetcher_cfg.get(key_field)
        if label:
            try:
                keyring.delete_password(svc, label)
            except keyring.errors.PasswordDeleteError:
                pass  # already absent

    print(f"Credentials deleted from OS keychain for '{bank_key}'.")


def generate_totp(totp_secret: str) -> str:
    """Return current 6-digit TOTP code."""
    import pyotp
    return pyotp.TOTP(totp_secret).now()
