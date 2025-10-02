from fastapi import APIRouter, HTTPException, Query, Depends
import httpx
import os
from pymongo import MongoClient
from utils.dependencies import verify_token

router = APIRouter(tags=["CMS Display Offer"])

# Strapi config
STRAPI_BASE_URL = "https://strapi-production-5603.up.railway.app/api/display-offer"
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")
if not STRAPI_BEARER_TOKEN:
    raise RuntimeError("STRAPI_BEARER_TOKEN environment variable must be set")

# Mongo config
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
locale_params_collection = db["Locale_Params"]


@router.get("/cms_display_offer")
async def cms_display_offer(
    locale: str = Query(..., example="en_GB"),
    _: None = Depends(verify_token)
):
    """
    Proxies Display Offer query to Strapi using strapi_locale (from Locale_Params).
    """
    # Step 1: Convert to strapi_locale
    locale_doc = locale_params_collection.find_one({"locale": locale})
    if not locale_doc or "strapi_locale" not in locale_doc:
        raise HTTPException(
            status_code=400,
            detail=f"Locale '{locale}' is not supported or has no strapi_locale mapping."
        )

    strapi_locale = locale_doc["strapi_locale"]

    params = {"locale": strapi_locale}
    headers = {"Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                STRAPI_BASE_URL,
                params=params,
                headers=headers,
                timeout=15.0,
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Strapi error: {response.text}"
            )

        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
