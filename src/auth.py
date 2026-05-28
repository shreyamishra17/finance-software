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


def create_user(first_name: str, last_name: str, email: str, password: str) -> dict:
    first_name = first_name.strip()
    last_name = last_name.strip()
    email_norm = email.strip().lower()
    if not first_name:
        raise ValueError("Please enter your first name")
    if not last_name:
        raise ValueError("Please enter your last name")
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
        "first_name": first_name,
        "last_name": last_name,
        "email": email_norm,
        "password_salt": salt,
        "password_hash": _hash_password(password, salt),
        "created_at": now,
        "updated_at": now,
    })
    return {"uid": uid, "first_name": first_name, "last_name": last_name, "email": email_norm}


def authenticate_user(email: str, password: str) -> dict:
    email_norm = email.strip().lower()
    docs = _users_collection().where("email", "==", email_norm).limit(1).get()
    if not docs:
        raise ValueError("This email does not exist. Please create an account.")
    user = docs[0].to_dict()
    expected = _hash_password(password, user["password_salt"])
    if not hmac.compare_digest(expected, user["password_hash"]):
        raise ValueError("Incorrect password")
    return {
        "uid": user["uid"],
        "first_name": user.get("first_name", ""),
        "last_name": user.get("last_name", ""),
        "email": user["email"],
    }


def update_password(uid: str, current_password: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValueError("New password must be at least 8 characters")
    doc = _users_collection().document(uid).get()
    if not doc.exists:
        raise ValueError("User not found")
    user = doc.to_dict()
    expected = _hash_password(current_password, user["password_salt"])
    if not hmac.compare_digest(expected, user["password_hash"]):
        raise ValueError("Current password is incorrect")
    new_salt = secrets.token_hex(16)
    _users_collection().document(uid).update({
        "password_salt": new_salt,
        "password_hash": _hash_password(new_password, new_salt),
        "updated_at": _utcnow().isoformat(),
    })


def update_profile(uid: str, first_name: str, last_name: str) -> dict:
    first_name = first_name.strip()
    last_name = last_name.strip()
    if len(first_name) > 120 or len(last_name) > 120:
        raise ValueError("Name is too long")
    _users_collection().document(uid).update({
        "first_name": first_name,
        "last_name": last_name,
        "updated_at": _utcnow().isoformat(),
    })
    return {"first_name": first_name, "last_name": last_name}


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
    doc = _users_collection().document(uid).get()
    if doc.exists:
        user = doc.to_dict()
        return {
            "uid": uid,
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "email": email,
        }
    return {"uid": uid, "first_name": "", "last_name": "", "email": email}


def _delete_user_doc(uid: str) -> dict[str, int]:
    db = _get_db()
    user_ref = _users_collection().document(uid)
    subcollections = [
        user_ref.collection("accounts"),
        user_ref.collection("transactions"),
        user_ref.collection("credentials"),
        user_ref.collection("_meta"),
    ]

    deleted: dict[str, int] = {}
    for coll in subcollections:
        docs = coll.get()
        deleted[coll.id] = len(docs)
        if not docs:
            continue
        batch = db.batch()
        count = 0
        for doc in docs:
            batch.delete(doc.reference)
            count += 1
            if count >= 400:
                batch.commit()
                batch = db.batch()
                count = 0
        if count:
            batch.commit()

    user_ref.delete()
    deleted["user"] = 1
    return deleted


def delete_user(uid: str, email: str | None = None) -> dict:
    deleted = _delete_user_doc(uid)

    # Safety cleanup for legacy/duplicate rows with same email.
    # This handles cases where UI says delete succeeded but signup still
    # fails because an old duplicate user row remains.
    email_norm = (email or "").strip().lower()
    if email_norm:
        extra_docs = _users_collection().where("email", "==", email_norm).get()
        extras_deleted = 0
        for doc in extra_docs:
            if doc.id == uid:
                continue
            _delete_user_doc(doc.id)
            extras_deleted += 1
        if extras_deleted:
            deleted["extra_users_with_same_email"] = extras_deleted

    return deleted
