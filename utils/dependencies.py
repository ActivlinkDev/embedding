import os
import hmac
import logging
import email.message
from typing import Optional
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

# A ServiceClient issued with a client_key is pinned to that tenant: it may not act on any other
# client's data. Set ENFORCE_CLIENT_KEY_SCOPE=false to log mismatches without rejecting them
# (useful when first pinning existing callers, to catch over-broad credentials before they 403).
ENFORCE_CLIENT_KEY_SCOPE = os.getenv("ENFORCE_CLIENT_KEY_SCOPE", "true").lower() == "true"

security = HTTPBearer()  # 👈 built-in bearer token scheme

# ClientKey is spelled several ways across the routers (ClientKey, clientKey, client_key,
# clientkey, Client_Key), so names are compared with case and underscores stripped.
_CLIENT_KEY_NAMES = {"clientkey"}
_MAX_BODY_DEPTH = 4


def _is_client_key_name(name: str) -> bool:
    return name.replace("_", "").lower() in _CLIENT_KEY_NAMES


def _collect_from_payload(node, depth: int, found: set) -> None:
    """Walk a decoded JSON body collecting every ClientKey-ish value, however it is nested."""
    if depth > _MAX_BODY_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and _is_client_key_name(key) and isinstance(value, str):
                found.add(value)
            else:
                _collect_from_payload(value, depth + 1, found)
    elif isinstance(node, list):
        for item in node:
            _collect_from_payload(item, depth + 1, found)


def _body_kind(request: Request) -> str:
    """How FastAPI will decode this body — mirrors the content-type logic in fastapi.routing,
    so the check can never inspect less than the route itself parses. Returns json/form/skip.

    Notably JSON covers every `application/*+json` structured syntax (merge-patch+json,
    vnd.api+json, …) and a body sent with no Content-Type at all, both of which FastAPI binds
    to a Pydantic model. multipart/form-data is deliberately skipped: parsing it would consume
    the upload stream.
    """
    content_type_value = request.headers.get("content-type")
    if not content_type_value:
        return "json"

    message = email.message.Message()
    message["content-type"] = content_type_value
    if message.get_content_maintype() != "application":
        return "skip"

    subtype = message.get_content_subtype()
    if subtype == "json" or subtype.endswith("+json"):
        return "json"
    if subtype == "x-www-form-urlencoded":
        return "form"
    return "skip"


async def _request_client_keys(request: Request) -> set:
    """Every ClientKey value this request is asking to act on: path, query, then body."""
    found: set = set()

    for params in (request.path_params, request.query_params):
        for key, value in params.items():
            if _is_client_key_name(key) and isinstance(value, str):
                found.add(value)

    kind = _body_kind(request)
    try:
        if kind == "json":
            # Starlette caches the body, so reading it here does not consume it for the route.
            _collect_from_payload(await request.json(), 0, found)
        elif kind == "form":
            for key, value in (await request.form()).items():
                if _is_client_key_name(key) and isinstance(value, str):
                    found.add(value)
    except Exception:
        # Unparseable or empty body: nothing to enforce against here, and the route's own
        # validation will reject it if the payload was genuinely malformed.
        pass

    return found


async def _enforce_client_key(request: Request, caller: dict) -> None:
    """A caller pinned to one client_key may only touch that client's data."""
    allowed = caller.get("client_key")
    if not allowed:
        return  # unpinned credential (first-party callers, legacy static token)

    requested = await _request_client_keys(request)
    mismatched = sorted(k for k in requested if k != allowed)
    if not mismatched:
        return

    logger.warning(
        "[AUTH-SCOPE] ServiceClient '%s' (client_key=%s) requested client_key(s) %s on %s%s",
        caller.get("client_id"),
        allowed,
        mismatched,
        request.url.path,
        "" if ENFORCE_CLIENT_KEY_SCOPE else " — not enforced (ENFORCE_CLIENT_KEY_SCOPE=false)",
    )
    if ENFORCE_CLIENT_KEY_SCOPE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Caller is scoped to ClientKey {allowed}",
        )


async def verify_token(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Authenticate a request via a signed JWT (preferred) or, during migration, the legacy
    static API_TOKEN. On success, the resolved caller identity/scopes are attached to
    request.state.caller for routes/dependencies (e.g. require_scope) that want to read them,
    and a caller pinned to a client_key is held to it.
    """
    token = credentials.credentials

    claims = None
    try:
        claims = decode_jwt(token)
    except jwt.PyJWTError:
        pass

    if claims is not None:
        caller = {
            "client_id": claims.get("sub"),
            "client_key": claims.get("client_key"),
            "scopes": claims.get("scopes") or [],
        }
        request.state.caller = caller
        await _enforce_client_key(request, caller)
        return

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


def caller_client_key(request: Request, _: None = Depends(verify_token)) -> Optional[str]:
    """The tenant this caller is pinned to, or None if it may act on any client.

    `_enforce_client_key` only constrains requests that *name* a ClientKey. Routes that reach
    tenant-owned data by another identifier — a record id, a customer id, or no filter at all —
    must apply this to their own queries, and 404 records whose owner does not match. Records
    with an empty/missing owner field are treated as not belonging to a pinned caller.
    """
    caller = getattr(request.state, "caller", {}) or {}
    return caller.get("client_key") or None


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
