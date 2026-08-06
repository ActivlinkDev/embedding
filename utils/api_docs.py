"""Reusable OpenAPI documentation fragments.

Everything here is *metadata only*: the values are handed to FastAPI's `responses=` and
`description=` arguments, which feed the generated schema at /docs and /openapi.json. Nothing in
this module runs while a request is being served, and nothing here validates, filters, or
otherwise touches a response body — so documenting an endpoint cannot change how it behaves.

Usage in a router:

    from utils.api_docs import AUTH_NOTE, error, secured

    @router.post(
        "/thing",
        summary="Create a thing",
        response_description="The created thing.",
        responses=secured(**{
            200: {"description": "...", "content": {"application/json": {"example": {...}}}},
            404: error("No such client.", "Invalid clientkey."),
        }),
    )
"""

from typing import Any, Dict


def error(description: str, detail: Any) -> Dict[str, Any]:
    """An error response documented in FastAPI's standard ``{"detail": ...}`` shape."""
    return {
        "description": description,
        "content": {"application/json": {"example": {"detail": detail}}},
    }


def json_response(description: str, example: Any) -> Dict[str, Any]:
    """A success response documented by one concrete JSON example."""
    return {
        "description": description,
        "content": {"application/json": {"example": example}},
    }


# --- Standard error responses -------------------------------------------------------------
# Wording mirrors the real behaviour of utils.dependencies.verify_token / _enforce_client_key.

UNAUTHORIZED = error(
    "No bearer token was supplied, or the token is malformed, expired, or signed with the "
    "wrong key. Obtain a fresh token from `POST /auth/token`.",
    "Invalid or expired token",
)

FORBIDDEN = error(
    "The caller is authenticated but not permitted to act on the `ClientKey` named in this "
    "request. A ServiceClient pinned to one tenant may only address that tenant; an unpinned "
    "ServiceClient needs the `clientkey:*` scope to address any tenant at all.",
    "Caller is scoped to ClientKey acme_uk_live",
)

VALIDATION_ERROR = {
    "description": (
        "The request failed schema validation — a mandatory field is missing, or a value has "
        "the wrong type. The `loc` array points at the offending field."
    ),
    "content": {
        "application/json": {
            "example": {
                "detail": [
                    {
                        "type": "missing",
                        "loc": ["body", "clientkey"],
                        "msg": "Field required",
                        "input": {},
                    }
                ]
            }
        }
    },
}

NOT_FOUND = error("No record matches the supplied identifier.", "Not found")

UPSTREAM_ERROR = error(
    "A third-party service this endpoint depends on failed or timed out.",
    "Upstream service unavailable",
)


def secured(extra: Dict[Any, Dict[str, Any]] = None) -> Dict[Any, Dict[str, Any]]:
    """The error responses every `Depends(verify_token)` endpoint can return.

    Extra responses are merged in and win over the defaults, so a route can document its own
    200/404/409/502 alongside the standard 401/403/422::

        responses=secured({404: error("Unknown device.", "Device not found")})
    """
    responses: Dict[Any, Dict[str, Any]] = {
        401: UNAUTHORIZED,
        403: FORBIDDEN,
        422: VALIDATION_ERROR,
    }
    if extra:
        responses.update(extra)
    return responses


# --- Prose fragments reused across endpoint descriptions ----------------------------------

AUTH_NOTE = """
**Authentication** — send `Authorization: Bearer <token>`, where the token comes from
`POST /auth/token`. A caller whose credential is pinned to a `ClientKey` may only address
that tenant's data; addressing another tenant's `ClientKey` returns `403`.
"""

TENANT_NOTE = """
**Tenant scoping** — `clientkey` identifies the tenant and must match an active `ClientKey`
record. It is checked twice: once by the auth layer (which rejects a `ClientKey` the caller is
not entitled to) and once by this endpoint (which rejects one that does not exist).
"""

LOCALE_NOTE = """
**Locale** — an underscore-separated language/region code such as `en_GB`, `fr_FR` or `de_DE`.
It must exist in `Locale_Params`; call `GET /locales` for the supported list.
"""

PLACEHOLDER_NOTE = """
Placeholder values are rejected as if the field were empty: a blank string, `null`, and the
literal string `"string"` (the value Swagger's *Try it out* pre-fills) all count as missing.
"""
