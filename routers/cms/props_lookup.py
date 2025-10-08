from fastapi import APIRouter, HTTPException, Query, Depends
from typing import List
import httpx
import os
from pymongo import MongoClient
from utils.dependencies import verify_token
from utils.locale import resolve_strapi_locale, LocaleNotSupportedError

router = APIRouter(tags=["Props Lookup"]) 

STRAPI_BASE_URL = "https://strapi-production-5603.up.railway.app/api/props"
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")
if not STRAPI_BEARER_TOKEN:
    raise RuntimeError("STRAPI_BEARER_TOKEN environment variable must be set")

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
locale_params_collection = db["Locale_Params"]

@router.get("/props_lookup")
async def props_lookup(
    locale: str = Query(..., example="es_ES"),
    product_ids: List[str] = Query(..., alias="product_ids[]", example=["EX1", "WF1"]),
    _: None = Depends(verify_token)
):
    locale_doc = locale_params_collection.find_one({"locale": locale})
    try:
        _, strapi_locale = resolve_strapi_locale(locale, locale_doc)
    except LocaleNotSupportedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    params = [("locale", strapi_locale), ("populate", "*")]
    for idx, pid in enumerate(product_ids):
        params.append((f"filters[Product_ID][$in][{idx}]", pid))

    headers = {"Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"}

    try:
        async with httpx.AsyncClient() as client_http:
            response = await client_http.get(STRAPI_BASE_URL, params=params, headers=headers, timeout=15.0)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f"Strapi error: {response.text}")
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
