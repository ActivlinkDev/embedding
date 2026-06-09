from fastapi import APIRouter, Query, HTTPException, Request
import os
import httpx
from utils.locale import map_fastapi_to_strapi

router = APIRouter(tags=["CMS"])

STRAPI_BASE = os.getenv("STRAPI_BASE_URL")
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")

# Route slug only — STRAPI_BASE_URL already contains the /api prefix.
PORTAL_ADMIN_ROUTE = "portal-admins"


@router.get("/cms_portal_admin")
async def cms_portal_admin(
    locale: str = Query(..., description="Locale for the portal-admin content type"),
    request: Request = None,
):
    """Serve the portal-admin Strapi single-type content for the given locale."""
    if not STRAPI_BASE:
        raise HTTPException(status_code=500, detail="STRAPI_BASE_URL not configured")

    strapi_locale = map_fastapi_to_strapi(locale)
    upstream = f"{STRAPI_BASE.rstrip('/')}/{PORTAL_ADMIN_ROUTE}"
    params = {"populate": "*", "locale": strapi_locale}

    headers = {}
    if STRAPI_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {STRAPI_BEARER_TOKEN}"
    elif request:
        incoming = request.headers.get("authorization")
        if incoming:
            headers["Authorization"] = incoming

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(upstream, params=params, headers=headers)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Error contacting Strapi: {e}")

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:1000]
        raise HTTPException(status_code=resp.status_code, detail=detail)

    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Invalid JSON from Strapi")
