from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import os, hashlib, hmac, secrets, datetime
from utils.dependencies import verify_token

router = APIRouter(prefix="/portal", tags=["Portal"])

_mongo_uri = os.getenv("MONGO_URI")
_client = MongoClient(_mongo_uri) if _mongo_uri else None
_db = _client["Activlink"] if _client else None
_users = _db["PortalUser"] if _db is not None else None
_keys = _db["ClientKey"] if _db is not None else None

# Best-effort index creation at import time. If it fails (e.g. DB temporarily
# unreachable), _index_ready stays False and _ensure_index() retries before
# the first write so we never insert without the unique constraint in place.
_index_ready = False
try:
    if _users is not None:
        _users.create_index("username", unique=True)
        _index_ready = True
except Exception as e:
    print(f"[portal] Could not create PortalUser index at startup (will retry before first write): {e}")


def _ensure_index() -> None:
    """Guarantee the unique index exists before any write. Raises 503 if it cannot."""
    global _index_ready
    if _index_ready:
        return
    if _users is None:
        return  # _get_collections() will raise 503 before we reach here
    try:
        _users.create_index("username", unique=True)
        _index_ready = True
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Database index unavailable, cannot accept writes safely: {e}",
        )


# ---------------------------------------------------------------------------
# Password helpers (PBKDF2-HMAC-SHA256, no external deps)
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"{salt}${h.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return hmac.compare_digest(h.hex(), expected)


def _get_collections():
    if _users is None or _keys is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    return _users, _keys


def _client_info(keys_col, client_key: str) -> dict:
    doc = keys_col.find_one({"ClientKey": client_key}, {"Client_ID": 1, "Source": 1, "_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Client key not found")
    return {"clientId": doc.get("Client_ID", ""), "source": doc.get("Source", "")}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    username: str
    clientKey: str
    clientId: str
    source: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    clientKey: str


class CreateUserResponse(BaseModel):
    username: str
    clientKey: str
    clientId: str
    source: str
    role: str
    created_at: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/login", response_model=LoginResponse)
def portal_login(body: LoginRequest, _: None = Depends(verify_token)):
    users_col, keys_col = _get_collections()
    user = users_col.find_one({"username": body.username})
    if not user or not _verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    info = _client_info(keys_col, user["client_key"])
    return LoginResponse(
        username=user["username"],
        clientKey=user["client_key"],
        **info,
    )


@router.post("/users", response_model=CreateUserResponse)
def create_portal_user(body: CreateUserRequest, _: None = Depends(verify_token)):
    _ensure_index()
    users_col, keys_col = _get_collections()
    info = _client_info(keys_col, body.clientKey)
    now = datetime.datetime.utcnow()
    doc = {
        "username": body.username,
        "password_hash": _hash_password(body.password),
        "client_key": body.clientKey,
        "client_id": info["clientId"],
        "source": info["source"],
        "role": "user",
        "created_at": now,
    }
    try:
        users_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Username already exists")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create user: {e}")
    return CreateUserResponse(
        username=body.username,
        clientKey=body.clientKey,
        clientId=info["clientId"],
        source=info["source"],
        role="user",
        created_at=now.isoformat() + "Z",
    )
