"""Cloud ID links, their email verification codes and the short-lived access grant.

A link is the only thing in a confirmation email that leads back to a record. It is a random
192-bit token (``secrets.token_urlsafe(24)``); only its SHA-256 is stored, and a link is bound
to the tenant (``client_id``) that sent it, so a token opened on another client's storefront
resolves to nothing.

The link alone shows the non-personal summary. Personal details need a 6-digit code emailed
to the address the link was sent to; a correct code yields a grant, an HMAC over the link id
and an expiry, which the trusted frontend keeps in an httpOnly cookie and forwards.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from typing import Optional

from bson import ObjectId

from routers.comms import store

LINK_KINDS = ("device", "contract", "registration")
OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
OTP_MAX_PER_HOUR = 5
GRANT_TTL_SECONDS = 30 * 60


def _secret() -> bytes:
    value = os.getenv("CLOUD_ACCESS_SECRET")
    if not value or len(value) < 32:
        raise RuntimeError("CLOUD_ACCESS_SECRET must be set (32+ characters) for Cloud ID verification")
    return value.encode("utf-8")


def create_link(kind: str, ref_id: str, client_id: str, client_key: str,
                email: Optional[str], locale: Optional[str]) -> str:
    """Store a new link and return its token. The token is never stored or logged."""
    if kind not in LINK_KINDS:
        raise ValueError(f"Unknown link kind {kind!r}")
    token = secrets.token_urlsafe(24)
    store.cloud_links.insert_one({
        "token_hash": store.token_hash(token),
        "kind": kind,
        "ref_id": ref_id,
        "client_id": client_id,
        "client_key": client_key,
        "email": email,
        "locale": locale,
        "revoked": False,
        "created_at": store.now(),
    })
    return token


def link_url(base: str, token: str, locale: Optional[str]) -> str:
    url = f"{base}/cloud/{token}"
    return f"{url}?locale={locale}" if locale else url


def resolve_link(token: str, client_id: Optional[str]) -> Optional[dict]:
    """The link for ``token`` if it exists, is live and belongs to ``client_id``.

    Unknown, revoked and other-tenant tokens are indistinguishable to the caller."""
    if not client_id or not isinstance(token, str) or not (16 <= len(token) <= 128):
        return None
    link = store.cloud_links.find_one({"token_hash": store.token_hash(token)})
    if not link or link.get("revoked") or link.get("client_id") != client_id:
        return None
    return link


# ---------------------------------------------------------------------------
# Verification codes
# ---------------------------------------------------------------------------

class OtpError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _code_hash(link_id: str, code: str) -> str:
    return hmac.new(_secret(), f"{link_id}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def issue_code(link: dict) -> str:
    """Create a code for ``link``, superseding any earlier one. Raises OtpError(429)."""
    link_id = str(link["_id"])
    now = store.now()
    recent = store.cloud_otp.count_documents({"link_id": link_id, "created_at": {"$gte": now - timedelta(hours=1)}})
    if recent >= OTP_MAX_PER_HOUR:
        raise OtpError(429, "rate_limited")
    code = f"{secrets.randbelow(1_000_000):06d}"
    store.cloud_otp.update_many({"link_id": link_id, "superseded": False}, {"$set": {"superseded": True}})
    store.cloud_otp.insert_one({
        "link_id": link_id,
        "code_hash": _code_hash(link_id, code),
        "attempts": 0,
        "superseded": False,
        "used": False,
        "created_at": now,
        "expires_at": now + OTP_TTL,
        # Kept for an hour, not ten minutes, so the hourly send limit can count it.
        "purge_at": now + timedelta(hours=1),
    })
    return code


def check_code(link: dict, code: str) -> None:
    """Consume a correct code. Raises OtpError for a wrong, expired or exhausted one."""
    link_id = str(link["_id"])
    if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
        raise OtpError(400, "incorrect")
    row = store.cloud_otp.find_one({"link_id": link_id, "superseded": False, "used": False},
                                   sort=[("created_at", -1)])
    if not row:
        raise OtpError(400, "expired")
    expires = row["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=store.now().tzinfo)
    if expires < store.now():
        raise OtpError(400, "expired")
    if row.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        raise OtpError(429, "too_many_attempts")
    if not hmac.compare_digest(row["code_hash"], _code_hash(link_id, code)):
        # Counted atomically and only while under the limit, so parallel guesses cannot
        # each see attempts=4 and all get a try.
        result = store.cloud_otp.update_one(
            {"_id": row["_id"], "attempts": {"$lt": OTP_MAX_ATTEMPTS}}, {"$inc": {"attempts": 1}})
        if not result.modified_count or row.get("attempts", 0) + 1 >= OTP_MAX_ATTEMPTS:
            raise OtpError(429, "too_many_attempts")
        raise OtpError(400, "incorrect")
    used = store.cloud_otp.update_one({"_id": row["_id"], "used": False}, {"$set": {"used": True}})
    if not used.modified_count:
        raise OtpError(400, "expired")


# ---------------------------------------------------------------------------
# Access grant
# ---------------------------------------------------------------------------

def _grant_sig(link_id: str, expires: int) -> str:
    return hmac.new(_secret(), f"grant:{link_id}.{expires}".encode("utf-8"), hashlib.sha256).hexdigest()


def issue_grant(link: dict, now_ts: int) -> tuple[str, int]:
    link_id = str(link["_id"])
    expires = now_ts + GRANT_TTL_SECONDS
    return f"{link_id}.{expires}.{_grant_sig(link_id, expires)}", expires


def grant_valid(link: dict, grant: Optional[str], now_ts: int) -> bool:
    if not grant or grant.count(".") != 2:
        return False
    link_id, expires_raw, sig = grant.split(".")
    if link_id != str(link["_id"]) or not ObjectId.is_valid(link_id) or not expires_raw.isdigit():
        return False
    expires = int(expires_raw)
    if expires < now_ts or expires > now_ts + GRANT_TTL_SECONDS:
        return False
    return hmac.compare_digest(sig, _grant_sig(link_id, expires))
