import os
import base64
import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

from utils.dependencies import verify_token

load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dseo", tags=["Enrichment"])

DATAFORSEO_LOGIN = os.getenv("DATAFORSEO_LOGIN")
DATAFORSEO_PASSWORD = os.getenv("DATAFORSEO_PASSWORD")
DATAFORSEO_TASK_URL = "https://api.dataforseo.com/v3/merchant/google/products/task_post"
# Base URL for this service — used to build the postback_url sent to DataforSEO.
# Example: https://api.activlink.io
DSEO_WEBHOOK_BASE_URL = os.getenv("DSEO_WEBHOOK_BASE_URL", "").rstrip("/")

mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["Activlink"]
locale_collection = db["Locale_Params"]
mastersku_collection = db["MasterSKU"]


def _auth_header() -> str:
    if not DATAFORSEO_LOGIN or not DATAFORSEO_PASSWORD:
        raise HTTPException(status_code=500, detail="Missing DATAFORSEO_LOGIN or DATAFORSEO_PASSWORD")
    token = base64.b64encode(f"{DATAFORSEO_LOGIN}:{DATAFORSEO_PASSWORD}".encode()).decode()
    return f"Basic {token}"


@router.post("/shopping", dependencies=[Depends(verify_token)])
async def create_dseo_shopping_task(
    masterSKUid: str = Query(..., description="MasterSKU ObjectId — used as tag and to build the search keyword"),
    locale: str = Query("en_GB", description="Locale code used to resolve google_domain and location_code"),
):
    """
    Submit a DataforSEO merchant/google/products task for the given MasterSKU.
    The postback_url is set to this service's /dseo/webhook endpoint so the
    result is delivered asynchronously once DataforSEO completes the task.
    """
    if not DSEO_WEBHOOK_BASE_URL:
        raise HTTPException(status_code=500, detail="DSEO_WEBHOOK_BASE_URL is not configured")

    # Resolve MasterSKU document to build keyword
    try:
        sku_doc = mastersku_collection.find_one({"_id": ObjectId(masterSKUid)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid masterSKUid format")

    if not sku_doc:
        raise HTTPException(status_code=404, detail=f"MasterSKU '{masterSKUid}' not found")

    make = (sku_doc.get("Make") or "").strip()
    model = (sku_doc.get("Model") or "").strip()
    keyword = f"{make} {model}".strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="MasterSKU has no Make/Model to build a keyword from")

    # Resolve locale params
    locale_doc = locale_collection.find_one({"locale": locale}) or {}
    google_domain = locale_doc.get("google_domain") or "google.co.uk"
    location_code = locale_doc.get("location_code") or 2826

    postback_url = f"{DSEO_WEBHOOK_BASE_URL}/dseo/webhook?id=$id"

    payload = [
        {
            "language_code": "en",
            "location_code": location_code,
            "keyword": keyword,
            "price_min": 5,
            "priority": 2,
            "se_domain": google_domain,
            "postback_url": postback_url,
            "postback_data": "advanced",
            "tag": masterSKUid,
        }
    ]

    logger.info("[dseo_shopping] Posting task keyword=%r locale=%s tag=%s", keyword, locale, masterSKUid)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                DATAFORSEO_TASK_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": _auth_header(),
                },
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error("[dseo_shopping] DataforSEO HTTP error: %s", e)
        raise HTTPException(status_code=502, detail=f"DataforSEO error: {e.response.status_code}")
    except httpx.RequestError as e:
        logger.error("[dseo_shopping] DataforSEO request error: %s", e)
        raise HTTPException(status_code=502, detail=f"DataforSEO request failed: {e}")

    result = response.json()
    logger.info("[dseo_shopping] Task submitted successfully tag=%s", masterSKUid)
    return JSONResponse(content=result)
