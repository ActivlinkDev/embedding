from fastapi import APIRouter, HTTPException, Query, Depends
import httpx
import os
from pymongo import MongoClient
from utils.dependencies import verify_token
from utils.locale import resolve_strapi_locale, LocaleNotSupportedError

router = APIRouter(tags=["CMS Display Offer"]) 

STRAPI_BASE_URL = "https://strapi-production-5603.up.railway.app/api/display-offer"
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")
if not STRAPI_BEARER_TOKEN:
    raise RuntimeError("STRAPI_BEARER_TOKEN environment variable must be set")

# Lazily create Mongo client at request time to avoid DNS/SRV lookups during module import
_mongo_client = None

def _get_locale_params_collection():
    global _mongo_client
    try:
        if _mongo_client is None:
            _mongo_client = MongoClient(os.getenv("MONGO_URI"), connect=False)
        db = _mongo_client["Activlink"]
        return db["Locale_Params"]
    except Exception:
        return None

@router.get("/cms_display_offer")
async def cms_display_offer(
    locale: str = Query(..., example="en_GB"),
    _: None = Depends(verify_token)
):
    locale_doc = None
    try:
        col = _get_locale_params_collection()
        if col is not None:
            locale_doc = col.find_one({"locale": locale})
    except Exception:
        locale_doc = None
    try:
        _, strapi_locale = resolve_strapi_locale(locale, locale_doc)
    except LocaleNotSupportedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    params = {"locale": strapi_locale}
    headers = {"Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"}

    try:
        async with httpx.AsyncClient() as client_http:
            response = await client_http.get(STRAPI_BASE_URL, params=params, headers=headers, timeout=15.0)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f"Strapi error: {response.text}")
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
