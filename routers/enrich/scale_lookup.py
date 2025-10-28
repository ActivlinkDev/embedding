import os
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Any
from pymongo import MongoClient
from dotenv import load_dotenv

router = APIRouter(prefix="/scale", tags=["ScaleSERP"])

SCALE_SERP_API_KEY = os.getenv("SCALE_SERP_API_KEY")
SCALE_SERP_BASE_URL = "https://api.scaleserp.com/search"

load_dotenv()
mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["Activlink"]
locale_collection = db["Locale_Params"]


class ScaleShoppingResponse(BaseModel):
    query: str
    locale: str
    title: Optional[str]
    merchant: Optional[str]
    price: Optional[str]
    price_value: Optional[float]
    currency: Optional[str]
    rating: Optional[float]
    reviews: Optional[int]
    link: Optional[str]
    image: Optional[str]
    gpc_id: Optional[str]   # ✅ added mapping for Google Product Category ID
    source: str = "ScaleSERP"
    # full product details fetched in a follow-up call using gpc_id (if available)
    product_details: Optional[dict[str, Any]] = None


@router.get("/shopping", response_model=ScaleShoppingResponse)
async def get_shopping_result(
    query: str = Query(..., description="Product search query"),
    locale: str = Query("en_GB", description="Locale for search results")
):
    """
    Proxy to ScaleSERP Shopping API.
    Returns a trimmed single-result payload including gpc_id.
    """
    if not SCALE_SERP_API_KEY:
        raise HTTPException(status_code=500, detail="Missing SCALE_SERP_API_KEY")

    # lookup locale params from DB; fall back to sensible defaults
    locale_doc = locale_collection.find_one({"locale": locale}) or {}
    gl_val = locale_doc.get("gl") or "uk"
    google_domain_val = locale_doc.get("google_domain") or "google.co.uk"
    hl_val = locale_doc.get("hl") or "en"
    engine_val = locale_doc.get("engine") or "google"

    params = {
        "api_key": SCALE_SERP_API_KEY,
        "search_type": "shopping",
        "q": query,
        "shopping_condition": "new",
        "gl": gl_val,
        "google_domain": google_domain_val,
        "hl": hl_val,
        "engine": engine_val,
        "num": 1,  # limit to 1 result
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(SCALE_SERP_BASE_URL, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except httpx.RequestError as e:
            raise HTTPException(status_code=500, detail=f"ScaleSERP request failed: {str(e)}")

    data = response.json()
    item = (data.get("shopping_results") or [None])[0] or {}

    trimmed = {
        "query": query,
        "locale": locale,
        "title": item.get("title"),
        "merchant": item.get("merchant"),
        "price": item.get("price_parsed", {}).get("raw") or item.get("price_raw"),
        "price_value": item.get("price_parsed", {}).get("value"),
        "currency": item.get("price_parsed", {}).get("currency"),
        "rating": item.get("rating"),
        "reviews": item.get("reviews"),
        "link": item.get("link"),
        "image": item.get("image"),
        "gpc_id": item.get("gpc_id"),   # ✅ new mapping
        "source": "ScaleSERP",
        "product_details": None,
    }

    # If gpc_id present, call ScaleSERP again to fetch product details
    gpc = trimmed.get("gpc_id")
    if gpc:
        product_params = {
            "api_key": SCALE_SERP_API_KEY,
            "search_type": "product_details",
            "gpc_id": gpc,
            # reuse locale-derived hints
            "engine": engine_val,
            "gl": gl_val,
            "google_domain": google_domain_val,
            "hl": hl_val,
        }
        try:
            # use a fresh client for the follow-up call (initial client may be closed)
            async with httpx.AsyncClient(timeout=10.0) as client2:
                pd_resp = await client2.get(SCALE_SERP_BASE_URL, params=product_params)
                pd_resp.raise_for_status()
                pd_data = pd_resp.json()
                # attach only the product_results object from the product_details response
                trimmed["product_details"] = pd_data.get("product_results")
        except httpx.HTTPStatusError as e:
            # don't fail the whole endpoint for product details failures; include error info
            trimmed["product_details"] = {"error": f"status {e.response.status_code}", "detail": str(e)}
        except httpx.RequestError as e:
            trimmed["product_details"] = {"error": "request_failed", "detail": str(e)}

    return JSONResponse(content=trimmed)
