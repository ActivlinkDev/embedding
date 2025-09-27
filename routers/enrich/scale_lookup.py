import os
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/scale", tags=["ScaleSERP"])

SCALE_SERP_API_KEY = os.getenv("SCALE_SERP_API_KEY")
SCALE_SERP_BASE_URL = "https://api.scaleserp.com/search"


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

    params = {
        "api_key": SCALE_SERP_API_KEY,
        "search_type": "shopping",
        "q": query,
        "shopping_condition": "new",
        "gl": "uk",  # hard-coded for now; could map locale -> gl if needed
        "google_domain": "google.co.uk",
        "engine": "google",
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
    }

    return JSONResponse(content=trimmed)
