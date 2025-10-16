from fastapi import APIRouter, Query, HTTPException, Request
import os
import httpx

router = APIRouter(tags=["CMS"])

STRAPI_BASE = os.getenv("STRAPI_BASE_URL")
if not STRAPI_BASE:
    # Allow app to import even if not configured; endpoint will return 500 if called without config
    STRAPI_BASE = None


@router.get("/cms/strapi")
async def proxy_strapi(route: str = Query(...), locale: str | None = Query(None), request: Request = None):
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
    params = {}
    if locale:
        params['locale'] = locale

    headers = {k: v for k, v in request.headers.items() if k.lower() in ('authorization', 'accept')}

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
