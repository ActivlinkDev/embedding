from fastapi import APIRouter, Query, HTTPException, Request
import os
import httpx

router = APIRouter(tags=["CMS"])

STRAPI_BASE = os.getenv("STRAPI_BASE_URL")
if not STRAPI_BASE:
    # Allow app to import even if not configured; endpoint will return 500 if called without config
    STRAPI_BASE = None

# Optional server-side token to authenticate to Strapi
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")


@router.get("/cms/strapi")
async def proxy_strapi(
    route: str = Query(...),
    locale: str | None = Query(None),
    filter_field: str | None = Query(None, description="Optional collection field to filter on (Strapi field name)"),
    filter_value: list[str] | str | None = Query(None, description="Value(s) to match for filter_field. Provide multiple values to use $in operator."),
    request: Request = None,
):
    """Proxy a GET request to Strapi.

    Query parameters:
      - route: path appended to the Strapi base URL (no leading slash required)
      - locale: optional locale passed as query param to Strapi

    Example: /cms/strapi?route=pages/home&locale=en
    Will request: {STRAPI_BASE}/pages/home?locale=en
    """
    if not STRAPI_BASE:
        raise HTTPException(status_code=500, detail="STRAPI_BASE_URL not configured")

    # Build upstream URL
    route_clean = route.lstrip('/')
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
