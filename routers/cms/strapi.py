from fastapi import APIRouter, Query, HTTPException, Request, Depends
import os
import httpx
from utils.api_docs import error
from utils.dependencies import verify_token

router = APIRouter(tags=["CMS"])

STRAPI_BASE = os.getenv("STRAPI_BASE_URL")
if not STRAPI_BASE:
    # Allow app to import even if not configured; endpoint will return 500 if called without config
    STRAPI_BASE = None

# Optional server-side token to authenticate to Strapi
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")


@router.get(
    "/cms/strapi",
    summary="Read any CMS collection through a controlled proxy",
    response_description="Strapi's response for the requested route.",
    responses={
        200: {
            "description": (
                "Strapi's response, forwarded unchanged. The shape is defined by the CMS "
                "content type, not by this API."
            ),
            "content": {"application/json": {}},
        },
        400: error(
            "`route` is not a relative Strapi path — absolute URLs and `..` segments are refused.",
            "Invalid Strapi route",
        ),
        500: error("`STRAPI_BASE_URL` is not configured on this deployment.", "STRAPI_BASE_URL not configured"),
    },
)
async def proxy_strapi(
    route: str = Query(
        ...,
        description=(
            "**Mandatory.** Relative Strapi API path, e.g. `pages/home`. A leading slash is "
            "optional. Absolute URLs and `..` segments are rejected with `400`."
        ),
        examples=["pages/home"],
    ),
    locale: str | None = Query(
        None,
        description="Locale passed straight to Strapi — use the **CMS spelling** (`en-GB`), not the API's `en_GB`.",
        examples=["en-GB"],
    ),
    filter_field: str | None = Query(
        None,
        description="Strapi field name to filter on. Has no effect without `filter_value`.",
        examples=["slug"],
    ),
    filter_value: list[str] | str | None = Query(
        None,
        description=(
            "Value to match. Repeat the parameter to match any of several values (`$in`); a "
            "single value uses `$eq`."
        ),
        examples=[["home"]],
    ),
    request: Request = None,
    _: None = Depends(verify_token),
):
    """
    Read any CMS collection through a controlled server-side proxy, for content that has no
    dedicated endpoint.

    `route` is mandatory and is appended to the configured Strapi base URL — so
    `?route=pages/home&locale=en-GB` fetches `{STRAPI_BASE}/pages/home?locale=en-GB`. Only
    relative paths are accepted: absolute URLs and `..` segments are rejected with `400`, so the
    proxy cannot be pointed at another host.

    **`locale` is passed through verbatim**, so use the CMS spelling (`en-GB`), unlike the other
    CMS endpoints which take `en_GB` and translate for you.

    Relations are always fully populated (`populate=*`). Filtering is optional: `filter_field`
    with one `filter_value` becomes an equality match, and repeating `filter_value` becomes an
    `$in` match.

    The CMS token is applied server-side, so the browser never sees it. The response is
    **Strapi's JSON, forwarded unchanged**.
    """
    if not STRAPI_BASE:
        raise HTTPException(status_code=500, detail="STRAPI_BASE_URL not configured")

    # Build upstream URL. Only allow relative Strapi API paths to prevent proxy abuse.
    route_clean = route.lstrip('/')
    if not route_clean or '..' in route_clean.split('/') or route_clean.startswith(('http://', 'https://', '//')):
        raise HTTPException(status_code=400, detail='Invalid Strapi route')
    upstream = f"{STRAPI_BASE.rstrip('/')}/{route_clean}"
    # Build query params for Strapi: include locale, optional filters, and always populate all relations
    params = {"populate": "*"}
    if locale:
        params['locale'] = locale
    # If both filter_field and filter_value provided, add Strapi filters[<field>][$eq]=<value>
    if filter_field and filter_value is not None:
        # If filter_value is a list or multiple query params, use $in operator;
        # otherwise use $eq.
        if isinstance(filter_value, (list, tuple)):
            # Strapi accepts comma-separated values for $in
            joined = ",".join(str(x) for x in filter_value)
            params[f"filters[{filter_field}][$in]"] = joined
        else:
            params[f"filters[{filter_field}][$eq]"] = str(filter_value)

    # Build headers: prefer a server-side bearer token if configured, otherwise forward incoming Authorization
    headers = {k: v for k, v in request.headers.items() if k.lower() in ('accept',)}
    if STRAPI_BEARER_TOKEN:
        headers['Authorization'] = f"Bearer {STRAPI_BEARER_TOKEN}"
    else:
        # forward incoming Authorization if no server token configured
        incoming_auth = request.headers.get('authorization')
        if incoming_auth:
            headers['Authorization'] = incoming_auth

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(upstream, params=params, headers=headers)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Error contacting Strapi: {e}")

    content_type = resp.headers.get('content-type', '')
    if resp.status_code >= 400:
        # try to return JSON error if present
        try:
            return resp.json()
        except Exception:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:1000])

    if 'application/json' in content_type:
        try:
            return resp.json()
        except Exception:
            raise HTTPException(status_code=502, detail='Invalid JSON from Strapi')

    # For non-JSON, return raw text
    return resp.text
