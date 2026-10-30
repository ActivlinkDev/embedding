"""Collections and small helpers shared by confirmation emails and the Cloud ID page.

- ``EmailLog``: one row per email we intend to send, keyed by ``dedupe_key`` (unique). A row is
  claimed *before* calling Brevo, so a Stripe webhook replay or a double-submitted form finds
  the claim and sends nothing. Rows hold a masked address only — never the raw address or
  the template params.
- ``ClientEmailConfig``: per-ClientKey email settings edited in the admin portal. Kept out of
  the ``ClientKey`` document on purpose: ``/get-client`` returns that document whole and the
  frontend caches it in the browser.
- ``CloudLinks``: the unguessable links in confirmation emails. Only a SHA-256 of the token is
  stored, so a database read does not hand out working links.
- ``CloudOtp``: email verification codes for those links, hashed, with a TTL purge.
"""
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

from email_validator import EmailNotValidError, validate_email

from utils.mongo import require_client

_db = require_client()["Activlink"]
email_log = _db["EmailLog"]
email_config = _db["ClientEmailConfig"]
cloud_links = _db["CloudLinks"]
cloud_otp = _db["CloudOtp"]
client_keys = _db["ClientKey"]
customers = _db["Customer"]
devices = _db["Devices"]
contracts = _db["Contracts"]
baskets = _db["Basket_Quotes"]
registrations = _db["Registrations"]

# The customer step invents this address when someone gives no email
# (frontend app/validate_customer). Sending to it would only bounce.
PLACEHOLDER_EMAIL_DOMAIN = "placeholder.local"


def ensure_indexes() -> None:
    email_log.create_index("dedupe_key", unique=True)
    email_log.create_index([("client_key", 1), ("kind", 1), ("created_at", -1)])
    email_config.create_index("client_key", unique=True)
    cloud_links.create_index("token_hash", unique=True)
    cloud_otp.create_index([("link_id", 1), ("created_at", -1)])
    cloud_otp.create_index("purge_at", expireAfterSeconds=0)


def now() -> datetime:
    return datetime.now(timezone.utc)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def deliverable_email(value) -> Optional[str]:
    """The normalised address, or None when it is missing, malformed or a placeholder."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        email = validate_email(value.strip(), check_deliverability=False).normalized
    except EmailNotValidError:
        return None
    if email.lower().endswith("@" + PLACEHOLDER_EMAIL_DOMAIN):
        return None
    return email


def mask_email(value: Optional[str]) -> str:
    """``paul.smith@gmail.com`` -> ``p***@g***.com``. Safe to log and to show before OTP."""
    if not value or "@" not in value:
        return ""
    local, _, domain = value.partition("@")
    host, dot, tld = domain.rpartition(".")
    if not host:
        host, dot, tld = domain, "", ""
    return f"{local[:1]}***@{host[:1]}***{dot}{tld}"


def mask_identifier(value) -> Optional[str]:
    """Keep the last four characters of a serial / IMEI / MAC."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= 4:
        return text
    return "•" * min(len(text) - 4, 8) + text[-4:]


def first_name(name) -> str:
    if not isinstance(name, str):
        return ""
    parts = name.strip().split()
    return parts[0] if parts else ""


_HEX_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$|^#[0-9A-Fa-f]{3}$")


def is_hex_colour(value) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOUR.match(value))
