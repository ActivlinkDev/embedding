"""Authenticate DataforSEO postbacks.

DataforSEO cannot sign its postbacks, so the URL we hand it carries the proof: when a task is
submitted we append `sig = HMAC(DSEO_WEBHOOK_SECRET, tag)` to its `postback_url`, where `tag`
is the MasterSKU id the task is for. A postback is accepted only when every task in it carries
that same tag and the signature matches. Someone who has not seen a postback URL we issued
cannot forge one, and a URL captured for one SKU cannot be replayed against another.
"""
import hashlib
import hmac
import os
from urllib.parse import quote


def _secret() -> str:
    return os.getenv("DSEO_WEBHOOK_SECRET", "")


def sign_tag(tag: str) -> str:
    secret = _secret()
    if not secret:
        raise ValueError("DSEO_WEBHOOK_SECRET is not configured")
    return hmac.new(secret.encode(), f"dseo-postback|{tag}".encode(), hashlib.sha256).hexdigest()


def postback_url(base_url: str, tag: str) -> str:
    """The postback URL for a task targeting `tag`. `$id` is substituted by DataforSEO."""
    return f"{base_url}/dseo/webhook?id=$id&sig={quote(sign_tag(tag), safe='')}"


def verify_tag(tag: str, sig: str | None) -> bool:
    if not tag or not sig or not _secret():
        return False
    return hmac.compare_digest(sign_tag(tag), sig)
