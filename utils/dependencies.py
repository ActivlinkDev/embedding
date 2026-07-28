import os
import hmac
import logging
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
import jwt

from utils.jwt_auth import decode_jwt, LEGACY_API_TOKEN

load_dotenv()

logger = logging.getLogger(__name__)

# During the migration off the single shared secret, the legacy static bearer token is still
# accepted alongside signed JWTs. Set ALLOW_LEGACY_API_TOKEN=false once every caller has moved
# to /auth/token + ServiceClient credentials.
ALLOW_LEGACY_API_TOKEN = os.getenv("ALLOW_LEGACY_API_TOKEN", "true").lower() == "true"

security = HTTPBearer()  # 👈 built-in bearer token scheme


def verify_token(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Authenticate a request via a signed JWT (preferred) or, during migration, the legacy
    static API_TOKEN. On success, the resolved caller identity/scopes are attached to
    request.state.caller for routes/dependencies (e.g. require_scope) that want to read them.
    """
    token = credentials.credentials

    try:
        claims = decode_jwt(token)
        request.state.caller = {
            "client_id": claims.get("sub"),
            "client_key": claims.get("client_key"),
            "scopes": claims.get("scopes") or [],
        }
        return
    except jwt.PyJWTError:
        pass

    if ALLOW_LEGACY_API_TOKEN and LEGACY_API_TOKEN and hmac.compare_digest(token, LEGACY_API_TOKEN):
        logger.warning(
            "[AUTH-DEPRECATED] Legacy static API_TOKEN used for %s — migrate this caller to "
            "POST /auth/token with a ServiceClient credential.",
            request.url.path,
        )
        request.state.caller = {"client_id": "legacy-static-token", "client_key": None, "scopes": ["*"]}
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing token",
    )


def require_scope(scope: str):
    """Dependency factory: on top of verify_token, require the caller to hold `scope`
    (or the wildcard "*", used by the legacy static token during migration)."""

    def _check(request: Request, _: None = Depends(verify_token)) -> dict:
        caller = getattr(request.state, "caller", {}) or {}
        scopes = caller.get("scopes", [])
        if scope not in scopes and "*" not in scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope: {scope}",
            )
        return caller

    return _check
