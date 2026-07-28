import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

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
    client_id: str
    client_secret: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    scopes: List[str]


@router.post("/token", response_model=TokenResponse)
def issue_token(body: TokenRequest):
    """OAuth2 client-credentials style grant: exchange a ServiceClient id/secret for a
    short-lived signed JWT. Intentionally has no verify_token dependency — the client_secret
    itself is the credential being verified here."""
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
    client_id: str = Field(..., description="Unique identifier for the caller, e.g. 'frontend' or 'AO-manufacturer'")
    scopes: List[str] = Field(
        ...,
        min_length=1,
        description=(
            "Scopes granted to this caller, e.g. ['sku:read']. Add 'clientkey:*' to reach every "
            "tenant; otherwise the caller reaches only the client_key below."
        ),
    )
    client_key: Optional[str] = Field(
        None,
        description=(
            "ClientKey.ClientKey this caller is bound to. Leave null only for callers holding "
            "'clientkey:*' — a null binding without that scope reaches no tenant data at all."
        ),
    )


class CreateClientResponse(BaseModel):
    client_id: str
    client_secret: str = Field(..., description="Shown once — only its hash is stored. Save it now.")
    scopes: List[str]
    client_key: Optional[str] = None


class ServiceClientSummary(BaseModel):
    client_id: str
    scopes: List[str] = []
    client_key: Optional[str] = None
    active: bool = True
    created_at: Optional[datetime.datetime] = None
    created_by: Optional[str] = None
    rotated_at: Optional[datetime.datetime] = None
    rotated_by: Optional[str] = None


class ListClientsResponse(BaseModel):
    clients: List[ServiceClientSummary]


class RotateSecretResponse(BaseModel):
    client_id: str
    client_secret: str = Field(..., description="Shown once — only its hash is stored. Save it now.")


class SetActiveRequest(BaseModel):
    active: bool


def _caller_name(caller: Dict[str, Any]) -> Optional[str]:
    return (caller or {}).get("client_id")


@router.post("/clients", response_model=CreateClientResponse, status_code=201)
def create_client(body: CreateClientRequest, caller: dict = Depends(require_scope(ADMIN_SCOPE))):
    """Issue a new ServiceClient credential. The secret is generated server-side and returned
    exactly once — only its PBKDF2 hash is stored."""
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


@router.get("/clients", response_model=ListClientsResponse)
def list_clients(_: dict = Depends(require_scope(ADMIN_SCOPE))):
    """List ServiceClients. Secrets are never returned — rotate if one is lost."""
    try:
        return ListClientsResponse(clients=list_service_clients())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/clients/{client_id}/rotate", response_model=RotateSecretResponse)
def rotate_client_secret(client_id: str, caller: dict = Depends(require_scope(ADMIN_SCOPE))):
    """Issue a replacement secret for an existing client. The old secret stops working
    immediately; JWTs already issued from it remain valid until they expire."""
    client_secret = generate_client_secret()
    try:
        found = rotate_service_client_secret(client_id, client_secret, rotated_by=_caller_name(caller))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail=f"ServiceClient '{client_id}' not found")

    return RotateSecretResponse(client_id=client_id, client_secret=client_secret)


@router.patch("/clients/{client_id}", response_model=ServiceClientSummary)
def set_client_active(client_id: str, body: SetActiveRequest, _: dict = Depends(require_scope(ADMIN_SCOPE))):
    """Enable or revoke a client. Revoking blocks new /auth/token calls; tokens already issued
    stay valid until they expire (at most ACCESS_TOKEN_EXPIRES_MINUTES).

    Revoking the last active admin:clients holder is refused with 409 — it would lock everyone
    out of credential management once the legacy static token is disabled.
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
