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

_index_ready = False
try:
    if _users is not None:
        _users.create_index("username", unique=True)
        _index_ready = True
except Exception as e:
    print(f"[portal] Could not create PortalUser index at startup (will retry before first write): {e}")


def _ensure_index() -> None:
    global _index_ready
    if _index_ready:
        return
    if _users is None:
        return
    try:
        _users.create_index("username", unique=True)
        _index_ready = True
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Database index unavailable, cannot accept writes safely: {e}",
        )


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


def _get_client_keys(keys_col, client_id: str) -> list:
    """Return all clientkeys for the given client_id as [{clientKey, source}]."""
    docs = list(keys_col.find({"Client_ID": client_id}, {"ClientKey": 1, "Source": 1, "_id": 0}))
    return [{"clientKey": d["ClientKey"], "source": d.get("Source", "")} for d in docs]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class ClientKeyEntry(BaseModel):
    clientKey: str
    source: str


class LoginResponse(BaseModel):
    username: str
    clientId: str
    clientKeys: list[ClientKeyEntry]


class CreateUserRequest(BaseModel):
    username: str
    password: str
    clientId: str  # Scoped to client, not a specific clientkey


class CreateUserResponse(BaseModel):
    username: str
    clientId: str
    role: str
    created_at: str


class PortalUserEntry(BaseModel):
    username: str
    clientId: str
    role: str
    created_at: str | None = None


class ListUsersResponse(BaseModel):
    users: list[PortalUserEntry]


class UpdateStylesRequest(BaseModel):
    clientKey: str
    styles: dict


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/login", response_model=LoginResponse)
def portal_login(body: LoginRequest, _: None = Depends(verify_token)):
    users_col, keys_col = _get_collections()
    user = users_col.find_one({"username": body.username})
    if not user or not _verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    client_id = user.get("client_id", "")
    client_keys = _get_client_keys(keys_col, client_id)
    if not client_keys:
        raise HTTPException(status_code=404, detail=f"No client keys found for client '{client_id}'")

    return LoginResponse(
        username=user["username"],
        clientId=client_id,
        clientKeys=client_keys,
    )


@router.put("/styles")
def update_client_styles(body: UpdateStylesRequest, _: None = Depends(verify_token)):
    _, keys_col = _get_collections()
    result = keys_col.update_one(
        {"ClientKey": body.clientKey},
        {"$set": {"Styles": body.styles}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"ok": True, "message": "Styles updated successfully"}


@router.post("/users", response_model=CreateUserResponse)
def create_portal_user(body: CreateUserRequest, _: None = Depends(verify_token)):
    _ensure_index()
    users_col, keys_col = _get_collections()

    # Validate that at least one clientkey exists for this client_id
    if not keys_col.find_one({"Client_ID": body.clientId}, {"_id": 1}):
        raise HTTPException(status_code=404, detail=f"Client ID '{body.clientId}' not found")

    now = datetime.datetime.utcnow()
    doc = {
        "username": body.username,
        "password_hash": _hash_password(body.password),
        "client_id": body.clientId,
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
        clientId=body.clientId,
        role="user",
        created_at=now.isoformat() + "Z",
    )


def _serialize_created_at(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.isoformat() + "Z"
    return str(value)


@router.get("/users", response_model=ListUsersResponse)
def list_portal_users(clientId: str, _: None = Depends(verify_token)):
    """List all portal users scoped to a single client."""
    users_col, _ = _get_collections()
    docs = users_col.find(
        {"client_id": clientId},
        {"username": 1, "client_id": 1, "role": 1, "created_at": 1, "_id": 0},
    ).sort("username", 1)
    users = [
        PortalUserEntry(
            username=d.get("username", ""),
            clientId=d.get("client_id", ""),
            role=d.get("role", "user"),
            created_at=_serialize_created_at(d.get("created_at")),
        )
        for d in docs
    ]
    return ListUsersResponse(users=users)


@router.delete("/users/{username}")
def delete_portal_user(username: str, clientId: str, _: None = Depends(verify_token)):
    """Delete a portal user. Scoped to the client to prevent cross-client deletes."""
    users_col, _ = _get_collections()
    result = users_col.delete_one({"username": username, "client_id": clientId})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True, "message": "User deleted successfully"}
