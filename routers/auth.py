from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from utils.jwt_auth import authenticate_service_client, issue_jwt, ACCESS_TOKEN_EXPIRES_MINUTES

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
