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

# Scope granting access to every tenant. Tenant access is fail-closed, so a caller with no
# client_key binding needs this to reach any tenant data at all — issuing a credential without
# a binding is then a credential that can do nothing, rather than one that can do everything.
CLIENT_KEY_WILDCARD_SCOPE = "clientkey:*"

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


def _is_unrestricted(caller: dict) -> bool:
    """Whether this caller may act across every tenant. "*" is the legacy static token's scope,
    kept working until ALLOW_LEGACY_API_TOKEN=false."""
    scopes = caller.get("scopes", [])
    return CLIENT_KEY_WILDCARD_SCOPE in scopes or "*" in scopes


def _reject_or_log(caller: dict, detail: str, log_args: tuple) -> None:
    logger.warning(*log_args)
    if ENFORCE_CLIENT_KEY_SCOPE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


async def _enforce_client_key(request: Request, caller: dict) -> None:
    """Tenant access is fail-closed: a caller reaches a ClientKey only by being pinned to it, or
    by holding CLIENT_KEY_WILDCARD_SCOPE. A credential with neither — client_key null and no
    wildcard — reaches no tenant data at all, so one issued without a binding by mistake fails
    closed rather than silently seeing every tenant's records.
    """
    allowed = caller.get("client_key")
    requested = await _request_client_keys(request)

    if not allowed:
        # Requests that name no ClientKey are not tenant-addressed (health, /auth/token,
        # category and locale lookups), so an unpinned caller is left alone there.
        if not requested or _is_unrestricted(caller):
            return
        _reject_or_log(
            caller,
            f"Caller is not bound to a ClientKey and lacks the '{CLIENT_KEY_WILDCARD_SCOPE}' scope",
            (
                "[AUTH-SCOPE] ServiceClient '%s' is unbound and lacks '%s'; requested client_key(s) "
                "%s on %s%s",
                caller.get("client_id"),
                CLIENT_KEY_WILDCARD_SCOPE,
                sorted(requested),
                request.url.path,
                "" if ENFORCE_CLIENT_KEY_SCOPE else " — not enforced (ENFORCE_CLIENT_KEY_SCOPE=false)",
            ),
        )
        return

    mismatched = sorted(k for k in requested if k != allowed)
    if not mismatched:
        return

    _reject_or_log(
        caller,
        f"Caller is scoped to ClientKey {allowed}",
        (
            "[AUTH-SCOPE] ServiceClient '%s' (client_key=%s) requested client_key(s) %s on %s%s",
            caller.get("client_id"),
            allowed,
            mismatched,
            request.url.path,
            "" if ENFORCE_CLIENT_KEY_SCOPE else " — not enforced (ENFORCE_CLIENT_KEY_SCOPE=false)",
        ),
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

    Returning None means "no tenant filter", so an unbound caller must earn it with
    CLIENT_KEY_WILDCARD_SCOPE. Without that scope it is refused here rather than handed an
    unfiltered query — the same fail-closed rule `_enforce_client_key` applies, for the routes
    where the tenant is never named in the request.
    """
    caller = getattr(request.state, "caller", {}) or {}
    pinned = caller.get("client_key") or None
    if pinned is None and ENFORCE_CLIENT_KEY_SCOPE and not _is_unrestricted(caller):
        logger.warning(
            "[AUTH-SCOPE] ServiceClient '%s' is unbound and lacks '%s'; refused unfiltered access to %s",
            caller.get("client_id"),
            CLIENT_KEY_WILDCARD_SCOPE,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Caller is not bound to a ClientKey and lacks the '{CLIENT_KEY_WILDCARD_SCOPE}' scope",
        )
    return pinned


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
