from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Optional, List
import httpx
import os
from pydantic import BaseModel

from utils.dependencies import verify_token

router = APIRouter(tags=["Locale Infos"])

STRAPI_BASE_URL = "https://strapi-production-5603.up.railway.app/api/locale-infos"
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")
if not STRAPI_BEARER_TOKEN:
    raise RuntimeError("STRAPI_BEARER_TOKEN environment variable must be set")


# ---------- Pydantic Models ----------
class LocaleAttributes(BaseModel):
    Lang_Code: str
    API_Locale: str
    DescriptionEN: str
    Strapi_Locale: str
    createdAt: str
    updatedAt: str
    publishedAt: str
    Description_Local: str
    Change_Lang: str
    Currency_Name: str
    Currency_Icon: str
    Stripe_Locale: Optional[str]


class LocaleInfo(BaseModel):
    id: int
    attributes: LocaleAttributes


class Pagination(BaseModel):
    page: int
    pageSize: int
    pageCount: int
    total: int


class LocaleInfoResponse(BaseModel):
    data: List[LocaleInfo]
    meta: dict


# ---------- Endpoint ----------
@router.get("/locale_infos", response_model=LocaleInfoResponse)
async def locale_infos(
    locale: Optional[str] = Query(None, description="Filter by locale (optional)"),
    _: None = Depends(verify_token)
):
    """
    Fetch locale-infos from Strapi and return the structured response.
    """
    params = {}
    if locale:
        params["locale"] = locale

    headers = {
        "Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(STRAPI_BASE_URL, params=params, headers=headers, timeout=15.0)

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f"Strapi error: {response.text}")

        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
