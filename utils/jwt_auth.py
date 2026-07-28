"""Service-to-service auth: ServiceClient credentials + short-lived JWT issuance/verification.

Replaces the single shared API_TOKEN with per-caller identity. Every caller (frontend,
internal scripts, partner/MCP integrations) gets its own ServiceClient record and exchanges
a client_id/client_secret for a short-lived signed JWT via POST /auth/token.
"""
import os
import hashlib
import hmac
import secrets
import datetime
from typing import Optional

import jwt
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

MONGO_URI = os.getenv("MONGO_URI")
_client = MongoClient(MONGO_URI) if MONGO_URI else None
_db = _client["Activlink"] if _client is not None else None
_service_clients = _db["ServiceClient"] if _db is not None else None

_index_ready = False


def _ensure_index() -> None:
    global _index_ready
    if _index_ready or _service_clients is None:
        return
    try:
        _service_clients.create_index("client_id", unique=True)
        _index_ready = True
    except Exception as e:
        print(f"[jwt_auth] Could not create ServiceClient index: {e}")


_ensure_index()

JWT_SIGNING_SECRET = os.getenv("JWT_SIGNING_SECRET")
JWT_ISSUER = os.getenv("JWT_ISSUER", "activlink-embedding")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRES_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRES_MINUTES", "45"))

# Legacy single shared secret, kept only as a migration fallback in utils/dependencies.py.
LEGACY_API_TOKEN = os.getenv("API_TOKEN")

if not JWT_SIGNING_SECRET and not LEGACY_API_TOKEN:
    raise RuntimeError(
        "Neither JWT_SIGNING_SECRET nor API_TOKEN is set. "
        "Set JWT_SIGNING_SECRET to a strong random value to issue/verify service tokens."
    )


# ---------------------------------------------------------------------------
# Secret hashing (same PBKDF2-HMAC-SHA256 pattern as routers/portal.py)
# ---------------------------------------------------------------------------

def _hash_secret(secret: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 260_000)
    return f"{salt}${h.hex()}"


def _verify_secret(secret: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    h = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 260_000)
    return hmac.compare_digest(h.hex(), expected)


# ---------------------------------------------------------------------------
# ServiceClient CRUD
# ---------------------------------------------------------------------------

def create_service_client(
    client_id: str,
    client_secret: str,
    scopes: list[str],
    client_key: Optional[str] = None,
    created_by: Optional[str] = None,
) -> dict:
    if _service_clients is None:
        raise RuntimeError("MONGO_URI not configured")
    _ensure_index()

    doc = {
        "client_id": client_id,
        "secret_hash": _hash_secret(client_secret),
        "client_key": client_key,
        "scopes": scopes,
        "active": True,
        "created_at": datetime.datetime.utcnow(),
        "created_by": created_by,
    }
    try:
        _service_clients.insert_one(doc)
    except DuplicateKeyError:
        raise ValueError(f"ServiceClient '{client_id}' already exists")
    return doc


def authenticate_service_client(client_id: str, client_secret: str) -> Optional[dict]:
    if _service_clients is None:
        return None
    doc = _service_clients.find_one({"client_id": client_id})
    if not doc or not doc.get("active", False):
        return None
    if not _verify_secret(client_secret, doc.get("secret_hash", "")):
        return None
    return doc


# ---------------------------------------------------------------------------
# JWT issuance / verification
# ---------------------------------------------------------------------------

def issue_jwt(client_id: str, client_key: Optional[str], scopes: list[str]) -> str:
    if not JWT_SIGNING_SECRET:
        raise RuntimeError("JWT_SIGNING_SECRET not configured; cannot issue tokens")
    now = datetime.datetime.utcnow()
    payload = {
        "sub": client_id,
        "client_key": client_key,
        "scopes": scopes,
        "iat": now,
        "exp": now + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRES_MINUTES),
        "iss": JWT_ISSUER,
    }
    return jwt.encode(payload, JWT_SIGNING_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    """Raises jwt.PyJWTError subclasses on any invalid/expired/mis-signed token."""
    if not JWT_SIGNING_SECRET:
        raise jwt.InvalidTokenError("JWT_SIGNING_SECRET not configured")
    return jwt.decode(
        token,
        JWT_SIGNING_SECRET,
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
        options={"require": ["exp", "iat", "sub"]},
    )
