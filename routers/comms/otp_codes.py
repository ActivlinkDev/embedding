"""Email verification codes for /my-registrations and /my-contracts.

Kept in Mongo (``EmailOtp``), unlike the SMS codes in ``routers/otp.py`` which live in process
memory, so they survive restarts and work across instances. Codes are 6 digits from
``secrets``, stored only as an HMAC keyed by ``EMAIL_OTP_SECRET``, valid for 10 minutes, with 5
attempts each and at most 5 sent per address per hour.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from routers.comms import store

OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
OTP_MAX_PER_HOUR = 5


class OtpError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def secret() -> bytes:
    value = os.getenv("EMAIL_OTP_SECRET")
    if not value or len(value) < 32:
        raise RuntimeError("EMAIL_OTP_SECRET must be set (32+ characters) for email verification codes")
    return value.encode("utf-8")


def subject_for(email: str) -> str:
    """Codes are filed under a hash of the normalised address, not the address itself."""
    return "email:" + hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _code_hash(subject: str, code: str) -> str:
    return hmac.new(secret(), f"{subject}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def sends_in_last_hour(subject: str) -> int:
    return store.email_otp.count_documents(
        {"subject": subject, "created_at": {"$gte": store.now() - timedelta(hours=1)}})


def issue_code(subject: str) -> str:
    """Create a code for ``subject``, superseding any earlier one. Raises OtpError(429)."""
    if sends_in_last_hour(subject) >= OTP_MAX_PER_HOUR:
        raise OtpError(429, "rate_limited")
    now = store.now()
    code = f"{secrets.randbelow(1_000_000):06d}"
    store.email_otp.update_many({"subject": subject, "superseded": False}, {"$set": {"superseded": True}})
    store.email_otp.insert_one({
        "subject": subject,
        "code_hash": _code_hash(subject, code),
        "attempts": 0,
        "superseded": False,
        "used": False,
        "created_at": now,
        "expires_at": now + OTP_TTL,
        # Kept for an hour, not ten minutes, so the hourly send limit can count it.
        "purge_at": now + timedelta(hours=1),
    })
    return code


def check_code(subject: str, code: str) -> None:
    """Consume a correct code. Raises OtpError for a wrong, expired or exhausted one."""
    if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
        raise OtpError(400, "incorrect")
    row = store.email_otp.find_one({"subject": subject, "superseded": False, "used": False},
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
    if not hmac.compare_digest(row["code_hash"], _code_hash(subject, code)):
        # Counted atomically and only while under the limit, so parallel guesses cannot
        # each see attempts=4 and all get a try.
        result = store.email_otp.update_one(
            {"_id": row["_id"], "attempts": {"$lt": OTP_MAX_ATTEMPTS}}, {"$inc": {"attempts": 1}})
        if not result.modified_count or row.get("attempts", 0) + 1 >= OTP_MAX_ATTEMPTS:
            raise OtpError(429, "too_many_attempts")
        raise OtpError(400, "incorrect")
    used = store.email_otp.update_one({"_id": row["_id"], "used": False}, {"$set": {"used": True}})
    if not used.modified_count:
        raise OtpError(400, "expired")
