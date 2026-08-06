from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import os, hashlib, hmac, secrets, datetime
from utils.api_docs import error, json_response, secured
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
    """Credentials of a portal (human) user. Both fields are mandatory."""

    username: str = Field(..., description="**Mandatory.** The portal user's username.", examples=["jane.okafor"])
    password: str = Field(
        ...,
        description="**Mandatory.** Plain-text password, checked against a stored PBKDF2 hash.",
        examples=["<your-portal-password>"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {"username": "jane.okafor", "password": "<your-portal-password>"}
        }
    }


class ClientKeyEntry(BaseModel):
    """One ClientKey the logged-in user may operate under."""

    clientKey: str = Field(..., description="The key to send as `clientkey` on other endpoints.", examples=["acme_uk_live"])
    source: str = Field(..., description="The channel this key represents, e.g. `web`, `pos`.", examples=["web"])


class LoginResponse(BaseModel):
    username: str = Field(..., description="The authenticated user.", examples=["jane.okafor"])
    clientId: str = Field(..., description="The client this user is scoped to.", examples=["ACME-UK"])
    clientKeys: list[ClientKeyEntry] = Field(
        ..., description="Every ClientKey belonging to that client. Never empty — login fails with `404` if there are none."
    )


class CreateUserRequest(BaseModel):
    """A new portal user, scoped to a client rather than to one specific ClientKey."""

    username: str = Field(
        ...,
        description="**Mandatory.** Must be unique across the whole portal; a clash returns `409`.",
        examples=["sam.reeves"],
    )
    password: str = Field(
        ...,
        description="**Mandatory.** Stored only as a PBKDF2-SHA256 hash with a per-user salt.",
        examples=["<your-portal-password>"],
    )
    clientId: str = Field(
        ...,
        description=(
            "**Mandatory.** The client the user belongs to. Must already have at least one "
            "ClientKey, otherwise `404`."
        ),
        examples=["ACME-UK"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "username": "sam.reeves",
                "password": "<your-portal-password>",
                "clientId": "ACME-UK",
            }
        }
    }


class CreateUserResponse(BaseModel):
    username: str = Field(..., description="The created user's username.", examples=["sam.reeves"])
    clientId: str = Field(..., description="The client they belong to.", examples=["ACME-UK"])
    role: str = Field(..., description="Always `user` — the API creates no other role.", examples=["user"])
    created_at: str = Field(..., description="UTC ISO-8601 timestamp.", examples=["2026-08-06T10:14:52.113000Z"])


class PortalUserEntry(BaseModel):
    username: str = Field(..., description="The user's username, unique portal-wide.", examples=["sam.reeves"])
    clientId: str = Field(..., description="The client they belong to.", examples=["ACME-UK"])
    role: str = Field(..., description="Always `user` for API-created accounts.", examples=["user"])
    created_at: str | None = Field(None, description="Null for users created before this was recorded.", examples=["2026-08-06T10:14:52.113000Z"])


class ListUsersResponse(BaseModel):
    users: list[PortalUserEntry] = Field(..., description="Users for the requested client, sorted by username.")


class UpdateStylesRequest(BaseModel):
    """Branding overrides stored against one ClientKey."""

    clientKey: str = Field(
        ...,
        description="**Mandatory.** The ClientKey whose styling is being replaced.",
        examples=["acme_uk_live"],
    )
    styles: dict = Field(
        ...,
        description=(
            "**Mandatory.** Free-form styling object. It **replaces** the stored `Styles` "
            "wholesale rather than merging, so send the complete set every time."
        ),
        examples=[{"primaryColour": "#0B5FFF", "fontFamily": "Inter, sans-serif", "logoUrl": "https://cdn.example.com/acme.svg"}],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "clientKey": "acme_uk_live",
                "styles": {
                    "primaryColour": "#0B5FFF",
                    "fontFamily": "Inter, sans-serif",
                    "logoUrl": "https://cdn.example.com/acme.svg",
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Authenticate a portal user",
    response_description="The user, the client they belong to, and every ClientKey they may use.",
    responses=secured({
        200: json_response(
            "Credentials accepted.",
            {
                "username": "jane.okafor",
                "clientId": "ACME-UK",
                "clientKeys": [
                    {"clientKey": "acme_uk_live", "source": "web"},
                    {"clientKey": "acme_uk_pos", "source": "pos"},
                ],
            },
        ),
        401: error("Unknown username or wrong password.", "Invalid credentials"),
        404: error(
            "The credentials are valid but the client has no ClientKey, so there is nothing "
            "the user could operate on.",
            "No client keys found for client 'ACME-UK'",
        ),
        503: error("`MONGO_URI` is not configured on this deployment.", "Database not configured"),
    }),
)
def portal_login(body: LoginRequest, _: None = Depends(verify_token)):
    """
    Authenticate a **portal user** (a human logging into the admin portal) and return the
    ClientKeys they are entitled to use.

    This is a second, separate identity layer: the request itself still needs a valid
    service bearer token, and this endpoint then verifies the end user's username and password
    on top of it. It issues **no token of its own** — the returned `clientKeys` are what the
    portal passes as `clientkey` on subsequent calls.

    Passwords are verified against a PBKDF2-SHA256 hash with a constant-time comparison. A wrong
    username and a wrong password are indistinguishable in the response, by design.
    """
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


@router.put(
    "/styles",
    summary="Replace the branding styles for a ClientKey",
    response_description="Confirmation that the styles were written.",
    responses=secured({
        200: json_response("The styles were replaced.", {"ok": True, "message": "Styles updated successfully"}),
        404: error("No ClientKey matches `clientKey`.", "Client not found"),
        503: error("`MONGO_URI` is not configured on this deployment.", "Database not configured"),
    }),
)
def update_client_styles(body: UpdateStylesRequest, _: None = Depends(verify_token)):
    """
    Store the branding used by the embedded widget and customer-facing pages for one ClientKey.

    The `styles` object **overwrites** whatever is stored — it is a `$set` of the whole field,
    not a merge — so always send the complete style set, not just the keys you are changing.

    Both `clientKey` and `styles` are mandatory. `styles` is deliberately free-form: no key
    names are validated here, so a typo will be stored happily and simply not take effect.
    """
    _, keys_col = _get_collections()
    result = keys_col.update_one(
        {"ClientKey": body.clientKey},
        {"$set": {"Styles": body.styles}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"ok": True, "message": "Styles updated successfully"}


@router.post(
    "/users",
    response_model=CreateUserResponse,
    summary="Create a portal user for a client",
    response_description="The created user. Passwords are never echoed back.",
    responses=secured({
        200: json_response(
            "The user was created.",
            {
                "username": "sam.reeves",
                "clientId": "ACME-UK",
                "role": "user",
                "created_at": "2026-08-06T10:14:52.113000Z",
            },
        ),
        404: error("No ClientKey exists for this `clientId`, so the user would have nothing to use.", "Client ID 'ACME-UK' not found"),
        409: error("That username is already taken (usernames are unique portal-wide).", "Username already exists"),
        500: error("The user could not be written.", "Failed to create user: ..."),
        503: error("The unique-username index is unavailable, so writes are refused rather than risking duplicates.", "Database index unavailable, cannot accept writes safely: ..."),
    }),
)
def create_portal_user(body: CreateUserRequest, _: None = Depends(verify_token)):
    """
    Create a portal user scoped to a client.

    The user is bound to a `clientId`, **not** to a single ClientKey — at login they receive
    every ClientKey belonging to that client. The client must already have at least one
    ClientKey, otherwise the request is rejected with `404`.

    The password is hashed with PBKDF2-SHA256 (260,000 iterations, per-user salt) before
    storage; the plain value is never persisted or returned. Every user is created with
    `role: "user"` — this endpoint cannot create privileged roles.

    Usernames are unique across the entire portal, enforced by a unique index. If that index
    cannot be created the request fails with `503` rather than risk duplicate usernames.
    """
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


@router.get(
    "/users",
    response_model=ListUsersResponse,
    summary="List the portal users of a client",
    response_description="Portal users for the requested client, sorted by username.",
    responses=secured({
        200: json_response(
            "The client's users. An unknown `clientId` is not an error — it simply returns an "
            "empty list.",
            {
                "users": [
                    {
                        "username": "jane.okafor",
                        "clientId": "ACME-UK",
                        "role": "user",
                        "created_at": "2026-01-14T09:32:11Z",
                    },
                    {
                        "username": "sam.reeves",
                        "clientId": "ACME-UK",
                        "role": "user",
                        "created_at": "2026-08-06T10:14:52.113000Z",
                    },
                ]
            },
        ),
        503: error("`MONGO_URI` is not configured on this deployment.", "Database not configured"),
    }),
)
def list_portal_users(
    clientId: str = Query(
        ...,
        description="**Mandatory.** Only users belonging to this client are returned.",
        examples=["ACME-UK"],
    ),
    _: None = Depends(verify_token),
):
    """
    List every portal user belonging to one client, sorted by username.

    `clientId` is mandatory and acts as the tenant boundary — there is no way to list users
    across clients. Password hashes are never included. A client with no users, or an id that
    does not exist, both return `{"users": []}`.
    """
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


@router.delete(
    "/users/{username}",
    summary="Delete a portal user",
    response_description="Confirmation that the user was removed.",
    responses=secured({
        200: json_response("The user was deleted.", {"ok": True, "message": "User deleted successfully"}),
        404: error(
            "No user matches that username **for that client** — the same response as a user "
            "that exists under a different client.",
            "User not found",
        ),
        503: error("`MONGO_URI` is not configured on this deployment.", "Database not configured"),
    }),
)
def delete_portal_user(
    username: str,
    clientId: str = Query(
        ...,
        description="**Mandatory.** The client the user must belong to. Guards against cross-client deletes.",
        examples=["ACME-UK"],
    ),
    _: None = Depends(verify_token),
):
    """
    Delete a portal user.

    Both the path parameter `username` and the query parameter `clientId` are mandatory, and the
    delete only matches when **both** agree: a user belonging to another client cannot be deleted
    by guessing their username, and that case is reported as `404` rather than `403`.

    The operation is idempotent in effect — deleting an already-deleted user returns `404`.
    """
    users_col, _ = _get_collections()
    result = users_col.delete_one({"username": username, "client_id": clientId})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True, "message": "User deleted successfully"}
