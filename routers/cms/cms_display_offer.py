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

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
locale_params_collection = db["Locale_Params"]

@router.get("/cms_display_offer")
async def cms_display_offer(
    locale: str = Query(..., example="en_GB"),
    _: None = Depends(verify_token)
):
    locale_doc = locale_params_collection.find_one({"locale": locale})
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
