import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.api_docs import error, json_response, secured
from utils.dependencies import require_scope
from utils.jwt_auth import (
    ACCESS_TOKEN_EXPIRES_MINUTES,
    # Scope required to manage ServiceClient credentials. The legacy static API_TOKEN holds the
    # wildcard "*" scope during migration, so it can bootstrap the first admin client — one more
    # reason to set ALLOW_LEGACY_API_TOKEN=false as soon as every caller has moved over.
    ADMIN_SCOPE,
    LastAdminClientError,
    authenticate_service_client,
    create_service_client,
    generate_client_secret,
    issue_jwt,
    list_service_clients,
    rotate_service_client_secret,
    set_service_client_active,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


class TokenRequest(BaseModel):
    """Credentials of a `ServiceClient` record, as issued by `POST /auth/clients`."""

    client_id: str = Field(
        ...,
        description="**Mandatory.** The caller's ServiceClient identifier.",
        examples=["frontend"],
    )
    client_secret: str = Field(
        ...,
        description=(
            "**Mandatory.** The secret shown once when the client was created or last rotated. "
            "Only its hash is stored, so a lost secret must be rotated rather than recovered."
        ),
        examples=["your-client-secret-from-auth-clients"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "client_id": "frontend",
                "client_secret": "your-client-secret-from-auth-clients",
            }
        }
    }


class TokenResponse(BaseModel):
    """A short-lived signed JWT plus the scopes it carries."""

    access_token: str = Field(
        ...,
        description="Signed HS256 JWT. Send it as `Authorization: Bearer <access_token>`.",
        examples=["header.payload.signature-returned-by-auth-token"],
    )
    token_type: str = Field(
        "bearer",
        description="Always `bearer`.",
        examples=["bearer"],
    )
    expires_in: int = Field(
        ...,
        description=(
            "Lifetime of the token in seconds, from `ACCESS_TOKEN_EXPIRES_MINUTES` "
            "(45 minutes by default). Re-request a token before this elapses."
        ),
        examples=[2700],
    )
    scopes: List[str] = Field(
        ...,
        description="Scopes granted to this credential. `clientkey:*` means every tenant.",
        examples=[["sku:read", "clientkey:*"]],
    )


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Exchange ServiceClient credentials for an access token",
    response_description="A signed JWT and the scopes it carries.",
    responses={
        200: json_response(
            "Credentials accepted; a fresh token is issued.",
            {
                "access_token": "header.payload.signature-returned-by-auth-token",
                "token_type": "bearer",
                "expires_in": 2700,
                "scopes": ["sku:read", "clientkey:*"],
            },
        ),
        401: error(
            "The client_id is unknown, the secret does not match, or the client has been "
            "deactivated.",
            "Invalid client_id or client_secret",
        ),
    },
)
def issue_token(body: TokenRequest):
    """
    Exchange a `ServiceClient` id and secret for a short-lived bearer token.

    This is the entry point for every other endpoint in the API: obtain a token here, then send
    it as `Authorization: Bearer <access_token>` on subsequent calls. The token is signed with
    `JWT_SIGNING_SECRET` and carries the client's `scopes` and, where the credential is pinned to
    one tenant, its `client_key`.

    **No authentication is required to call this endpoint** — the `client_secret` in the body
    *is* the credential being verified. Every other endpoint requires the token it returns.

    Tokens expire after `ACCESS_TOKEN_EXPIRES_MINUTES` (45 by default) and cannot be refreshed;
    request a new one with the same credentials. See `docs/service-clients.md` for how to create
    a ServiceClient record.
    """
    client = authenticate_service_client(body.client_id, body.client_secret)
    if not client:
        raise HTTPException(status_code=401, detail="Invalid client_id or client_secret")

    scopes: List[str] = client.get("scopes", [])
    token = issue_jwt(
        client_id=client["client_id"],
        client_key=client.get("client_key"),
        scopes=scopes,
    )
    return TokenResponse(
        access_token=token,
        expires_in=ACCESS_TOKEN_EXPIRES_MINUTES * 60,
        scopes=scopes,
    )


# ---------------------------------------------------------------------------
# ServiceClient administration — the API equivalent of scripts/issue_service_client.py
# ---------------------------------------------------------------------------

class CreateClientRequest(BaseModel):
    """A new caller credential. `client_id` and `scopes` are mandatory; `client_key` is optional
    but omitting it grants no tenant access unless `clientkey:*` is in `scopes`."""

    client_id: str = Field(
        ...,
        description=(
            "**Mandatory.** Unique identifier for the caller, e.g. `frontend` or "
            "`AO-manufacturer`. Rejected with `422` if blank and `409` if already taken."
        ),
        examples=["AO-manufacturer"],
    )
    scopes: List[str] = Field(
        ...,
        min_length=1,
        description=(
            "**Mandatory, at least one entry.** Scopes granted to this caller, e.g. "
            "`['sku:read']`. Add `clientkey:*` to reach every tenant; otherwise the caller "
            "reaches only the `client_key` below. `admin:clients` permits managing credentials."
        ),
        examples=[["sku:read", "device:write"]],
    )
    client_key: Optional[str] = Field(
        None,
        description=(
            "Optional. The `ClientKey.ClientKey` this caller is pinned to. Leave `null` only for "
            "callers holding `clientkey:*` — a null binding without that scope reaches no tenant "
            "data at all."
        ),
        examples=["acme_uk_live"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "client_id": "AO-manufacturer",
                "scopes": ["sku:read", "device:write"],
                "client_key": "acme_uk_live",
            }
        }
    }


class CreateClientResponse(BaseModel):
    """The created credential. The secret appears here and nowhere else, ever."""

    client_id: str = Field(..., description="The identifier the caller authenticates with.", examples=["AO-manufacturer"])
    client_secret: str = Field(
        ...,
        description="Shown once — only its hash is stored. Save it now.",
        examples=["generated-secret-shown-once-save-it-now"],
    )
    scopes: List[str] = Field(..., description="Scopes granted, as requested.", examples=[["sku:read", "device:write"]])
    client_key: Optional[str] = Field(None, description="The tenant this credential is pinned to, or `null`.", examples=["acme_uk_live"])


class ServiceClientSummary(BaseModel):
    """A stored credential, without its secret."""

    client_id: str = Field(..., description="The credential's identifier.", examples=["AO-manufacturer"])
    scopes: List[str] = Field([], description="Scopes this credential holds.", examples=[["sku:read", "device:write"]])
    client_key: Optional[str] = Field(None, description="The tenant it is pinned to, or `null` for unpinned.", examples=["acme_uk_live"])
    active: bool = Field(
        True,
        description="`false` means `POST /auth/token` refuses this credential.",
        examples=[True],
    )
    created_at: Optional[datetime.datetime] = Field(None, description="When the credential was created.", examples=["2026-01-14T09:32:11Z"])
    created_by: Optional[str] = Field(
        None,
        description="`client_id` of the admin caller that created this credential.",
        examples=["frontend-admin"],
    )
    rotated_at: Optional[datetime.datetime] = Field(None, description="When the secret was last rotated. `null` if never.", examples=["2026-05-02T11:04:56Z"])
    rotated_by: Optional[str] = Field(None, description="`client_id` of the admin caller that rotated it.", examples=["frontend-admin"])


class ListClientsResponse(BaseModel):
    clients: List[ServiceClientSummary] = Field(..., description="Every stored credential, revoked ones included.")


class RotateSecretResponse(BaseModel):
    client_id: str = Field(..., description="The credential whose secret was replaced.", examples=["AO-manufacturer"])
    client_secret: str = Field(
        ...,
        description="Shown once — only its hash is stored. Save it now.",
        examples=["replacement-secret-shown-once-save-it-now"],
    )


class SetActiveRequest(BaseModel):
    active: bool = Field(
        ...,
        description=(
            "**Mandatory.** `true` re-enables the credential, `false` revokes it so no new "
            "tokens can be issued for it."
        ),
        examples=[False],
    )


def _caller_name(caller: Dict[str, Any]) -> Optional[str]:
    return (caller or {}).get("client_id")


@router.post(
    "/clients",
    response_model=CreateClientResponse,
    status_code=201,
    summary="Create a ServiceClient credential",
    response_description="The new credential, including its secret (shown only here).",
    responses=secured({
        201: json_response(
            "Credential created.",
            {
                "client_id": "AO-manufacturer",
                "client_secret": "generated-secret-shown-once-save-it-now",
                "scopes": ["sku:read", "device:write"],
                "client_key": "acme_uk_live",
            },
        ),
        409: error("A ServiceClient with this client_id already exists.", "client_id already exists"),
        503: error("The credential store (MongoDB) is unreachable.", "MONGO_URI not configured"),
    }),
)
def create_client(body: CreateClientRequest, caller: dict = Depends(require_scope(ADMIN_SCOPE))):
    """
    Issue a new `ServiceClient` credential.

    The secret is generated server-side and returned **exactly once** in this response — only its
    PBKDF2 hash is stored, so it cannot be retrieved later. If it is lost, rotate the client.

    Requires the `admin:clients` scope.
    """
    client_id = body.client_id.strip()
    if not client_id:
        raise HTTPException(status_code=422, detail="client_id must not be empty")

    client_secret = generate_client_secret()
    try:
        create_service_client(
            client_id=client_id,
            client_secret=client_secret,
            scopes=body.scopes,
            client_key=body.client_key,
            created_by=_caller_name(caller),
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return CreateClientResponse(
        client_id=client_id,
        client_secret=client_secret,
        scopes=body.scopes,
        client_key=body.client_key,
    )


@router.get(
    "/clients",
    response_model=ListClientsResponse,
    summary="List ServiceClient credentials",
    response_description="Every stored credential, without secrets.",
    responses=secured({
        200: json_response(
            "The full list of credentials.",
            {
                "clients": [
                    {
                        "client_id": "AO-manufacturer",
                        "scopes": ["sku:read", "device:write"],
                        "client_key": "acme_uk_live",
                        "active": True,
                        "created_at": "2026-01-14T09:32:11Z",
                        "created_by": "frontend-admin",
                        "rotated_at": None,
                        "rotated_by": None,
                    }
                ]
            },
        ),
        503: error("The credential store (MongoDB) is unreachable.", "MONGO_URI not configured"),
    }),
)
def list_clients(_: dict = Depends(require_scope(ADMIN_SCOPE))):
    """
    List every `ServiceClient`, including revoked ones (`active: false`).

    Secrets are never returned — only their hashes are stored. If a secret is lost, rotate the
    credential instead of trying to recover it.

    Requires the `admin:clients` scope.
    """
    try:
        return ListClientsResponse(clients=list_service_clients())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post(
    "/clients/{client_id}/rotate",
    response_model=RotateSecretResponse,
    summary="Rotate a ServiceClient secret",
    response_description="The replacement secret (shown only here).",
    responses=secured({
        200: json_response(
            "A new secret was generated and stored.",
            {"client_id": "AO-manufacturer", "client_secret": "replacement-secret-shown-once-save-it-now"},
        ),
        404: error("No ServiceClient with this client_id.", "ServiceClient 'AO-manufacturer' not found"),
        503: error("The credential store (MongoDB) is unreachable.", "MONGO_URI not configured"),
    }),
)
def rotate_client_secret(client_id: str, caller: dict = Depends(require_scope(ADMIN_SCOPE))):
    """
    Issue a replacement secret for an existing client — the remedy for a lost or leaked secret.

    The old secret stops working immediately. JWTs already issued from it **remain valid until
    they expire** (at most `ACCESS_TOKEN_EXPIRES_MINUTES`); to cut a compromised caller off at
    once, deactivate it with `PATCH /auth/clients/{client_id}` as well.

    Path parameter `client_id` is mandatory. Requires the `admin:clients` scope.
    """
    client_secret = generate_client_secret()
    try:
        found = rotate_service_client_secret(client_id, client_secret, rotated_by=_caller_name(caller))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail=f"ServiceClient '{client_id}' not found")

    return RotateSecretResponse(client_id=client_id, client_secret=client_secret)


@router.patch(
    "/clients/{client_id}",
    response_model=ServiceClientSummary,
    summary="Activate or revoke a ServiceClient",
    response_description="The credential's state after the change.",
    responses=secured({
        200: json_response(
            "The credential was updated.",
            {
                "client_id": "AO-manufacturer",
                "scopes": ["sku:read", "device:write"],
                "client_key": "acme_uk_live",
                "active": False,
                "created_at": "2026-01-14T09:32:11Z",
                "created_by": "frontend-admin",
                "rotated_at": None,
                "rotated_by": None,
            },
        ),
        404: error("No ServiceClient with this client_id.", "ServiceClient 'AO-manufacturer' not found"),
        409: error(
            "Refused: this is the last active holder of the admin:clients scope.",
            "Refusing to deactivate the last active admin:clients ServiceClient",
        ),
        503: error("The credential store (MongoDB) is unreachable.", "MONGO_URI not configured"),
    }),
)
def set_client_active(client_id: str, body: SetActiveRequest, _: dict = Depends(require_scope(ADMIN_SCOPE))):
    """
    Enable or revoke a credential.

    Revoking (`active: false`) blocks new `POST /auth/token` calls immediately. Tokens already
    issued stay valid until they expire — at most `ACCESS_TOKEN_EXPIRES_MINUTES`.

    Revoking the **last active `admin:clients` holder** is refused with `409`: it would lock
    everyone out of credential management once the legacy static token is disabled.

    Path parameter `client_id` and body field `active` are both mandatory. Requires the
    `admin:clients` scope.
    """
    try:
        found = set_service_client_active(client_id, body.active)
        if not found:
            raise HTTPException(status_code=404, detail=f"ServiceClient '{client_id}' not found")
        updated = next((c for c in list_service_clients() if c["client_id"] == client_id), None)
    except LastAdminClientError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"ServiceClient '{client_id}' not found")
    return ServiceClientSummary(**updated)
