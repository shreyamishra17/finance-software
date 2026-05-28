from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from db import _get_db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad)


def _secret() -> bytes:
    env = os.environ.get("FIFO_AUTH_SECRET", "").strip()
    if env:
        return env.encode("utf-8")

    db = _get_db()
    ref = db.collection("_meta").document("auth")
    snap = ref.get()
    if snap.exists:
        val = str(snap.get("secret") or "").strip()
        if val:
            return val.encode("utf-8")

    generated = secrets.token_urlsafe(48)
    ref.set({"secret": generated}, merge=True)
    return generated.encode("utf-8")


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        200_000,
    ).hex()


def _users_collection():
    return _get_db().collection("users")


def create_user(email: str, password: str) -> dict:
    email_norm = email.strip().lower()
    if not email_norm or "@" not in email_norm:
        raise ValueError("Please enter a valid email address")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")

    existing = _users_collection().where("email", "==", email_norm).limit(1).get()
    if existing:
        raise ValueError("An account with this email already exists")

    uid = uuid.uuid4().hex
    salt = secrets.token_hex(16)
    now = _utcnow().isoformat()
    _users_collection().document(uid).set({
        "uid": uid,
        "email": email_norm,
        "password_salt": salt,
        "password_hash": _hash_password(password, salt),
        "created_at": now,
        "updated_at": now,
    })
    return {"uid": uid, "email": email_norm}


def authenticate_user(email: str, password: str) -> dict:
    email_norm = email.strip().lower()
    docs = _users_collection().where("email", "==", email_norm).limit(1).get()
    if not docs:
        raise ValueError("Invalid email or password")
    user = docs[0].to_dict()
    expected = _hash_password(password, user["password_salt"])
    if not hmac.compare_digest(expected, user["password_hash"]):
        raise ValueError("Invalid email or password")
    return {"uid": user["uid"], "email": user["email"]}


def issue_token(uid: str, email: str, days_valid: int = 30) -> str:
    payload = {
        "uid": uid,
        "email": email,
        "exp": int((_utcnow() + timedelta(days=days_valid)).timestamp()),
    }
    payload_raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64url_encode(payload_raw)
    sig = _b64url_encode(hmac.new(_secret(), body.encode("utf-8"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str) -> dict:
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Malformed token") from exc
    expected = _b64url_encode(hmac.new(_secret(), body.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Invalid token signature")
    payload = json.loads(_b64url_decode(body))
    if int(payload.get("exp", 0)) < int(_utcnow().timestamp()):
        raise ValueError("Session expired")
    uid = str(payload.get("uid") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    if not uid or not email:
        raise ValueError("Invalid token payload")
    return {"uid": uid, "email": email}
