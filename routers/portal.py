from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from pymongo import MongoClient
import os, hashlib, hmac, secrets, datetime
from utils.dependencies import verify_token

router = APIRouter(prefix="/portal", tags=["Portal"])

_client = MongoClient(os.getenv("MONGO_URI"))
_db = _client["Activlink"]
_users = _db["PortalUser"]
_keys = _db["ClientKey"]

# Ensure unique index on username
_users.create_index("username", unique=True)


# ---------------------------------------------------------------------------
# Password helpers (PBKDF2-HMAC-SHA256, no external deps)
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    """Return 'salt$hash' hex string."""
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


def _client_info(client_key: str) -> dict:
    doc = _keys.find_one({"ClientKey": client_key}, {"Client_ID": 1, "Source": 1, "_id": 0})
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
    user = _users.find_one({"username": body.username})
    if not user or not _verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    info = _client_info(user["client_key"])
    return LoginResponse(
        username=user["username"],
        clientKey=user["client_key"],
        **info,
    )


@router.post("/users", response_model=CreateUserResponse)
def create_portal_user(body: CreateUserRequest, _: None = Depends(verify_token)):
    info = _client_info(body.clientKey)
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
        _users.insert_one(doc)
    except Exception:
        raise HTTPException(status_code=409, detail="Username already exists")
    return CreateUserResponse(
        username=body.username,
        clientKey=body.clientKey,
        clientId=info["clientId"],
        source=info["source"],
        role="user",
        created_at=now.isoformat() + "Z",
    )
