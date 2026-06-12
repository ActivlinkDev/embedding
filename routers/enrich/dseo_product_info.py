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
DATAFORSEO_PRODUCT_INFO_URL = "https://api.dataforseo.com/v3/merchant/google/product_info/task_post"
DSEO_WEBHOOK_BASE_URL = os.getenv("DSEO_WEBHOOK_BASE_URL", "").rstrip("/")

mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["Activlink"]
locale_collection = db["Locale_Params"]
mastersku_collection = db["MasterSKU"]


def _auth_header() -> str:
    if not DATAFORSEO_LOGIN or not DATAFORSEO_PASSWORD:
        raise ValueError("Missing DATAFORSEO_LOGIN or DATAFORSEO_PASSWORD")
    token = base64.b64encode(f"{DATAFORSEO_LOGIN}:{DATAFORSEO_PASSWORD}".encode()).decode()
    return f"Basic {token}"


async def submit_dseo_product_info_task(masterSKUid: str, locale: str) -> dict:
    """
    Submit a DataforSEO merchant/google/product_info task for the given MasterSKU locale.
    Requires Product_ID to already be present in Locale_Specific_Data (populated by the
    shopping task webhook). Raises ValueError for config/validation errors.
    """
    if not DSEO_WEBHOOK_BASE_URL:
        raise ValueError("DSEO_WEBHOOK_BASE_URL is not configured")

    try:
        ms_id = ObjectId(masterSKUid)
    except Exception:
        raise ValueError(f"Invalid masterSKUid format: {masterSKUid!r}")

    ms_doc = mastersku_collection.find_one({"_id": ms_id})
    if not ms_doc:
        raise ValueError(f"MasterSKU '{masterSKUid}' not found")

    # Find Product_ID in the locale-specific data entry
    product_id = None
    for entry in ms_doc.get("Locale_Specific_Data") or []:
        if entry.get("locale") == locale:
            product_id = entry.get("Product_ID")
            break

    if not product_id:
        raise ValueError(
            f"No Product_ID found for MasterSKU '{masterSKUid}' locale '{locale}' — "
            "run the shopping task first"
        )

    locale_doc = locale_collection.find_one({"locale": locale})
    if not locale_doc:
        raise ValueError(f"Locale '{locale}' not found in Locale_Params")

    google_domain = locale_doc.get("google_domain") or "google.co.uk"
    location_code = locale_doc.get("location_code") or 2826
    language_code = locale_doc.get("hl") or "en"

    postback_url = f"{DSEO_WEBHOOK_BASE_URL}/dseo/webhook?id=$id"

    payload = [
        {
            "language_code": language_code,
            "location_code": location_code,
            "product_id": product_id,
            "priority": 2,
            "se_domain": google_domain,
            "postback_url": postback_url,
            "postback_data": "advanced",
            "tag": masterSKUid,
        }
    ]

    logger.info(
        "[dseo_product_info] Posting task product_id=%s locale=%s tag=%s",
        product_id, locale, masterSKUid,
    )

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            DATAFORSEO_PRODUCT_INFO_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": _auth_header(),
            },
        )
        response.raise_for_status()

    result = response.json()
    logger.info("[dseo_product_info] Task submitted successfully tag=%s", masterSKUid)
    return result


@router.post("/product_info", dependencies=[Depends(verify_token)])
async def create_dseo_product_info_task(
    masterSKUid: str = Query(..., description="MasterSKU ObjectId"),
    locale: str = Query("en_GB", description="Locale code — Product_ID must already exist for this locale"),
):
    """
    Submit a DataforSEO merchant/google/product_info task for the given MasterSKU.
    The Product_ID stored in Locale_Specific_Data (from the shopping task) is used.
    Results are delivered via the shared /dseo/webhook endpoint.
    """
    try:
        result = await submit_dseo_product_info_task(masterSKUid, locale)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "Invalid masterSKUid" in msg or "No Product_ID" in msg:
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=500, detail=msg)
    except httpx.HTTPStatusError as e:
        logger.error("[dseo_product_info] DataforSEO HTTP error: %s", e)
        raise HTTPException(status_code=502, detail=f"DataforSEO error: {e.response.status_code}")
    except httpx.RequestError as e:
        logger.error("[dseo_product_info] DataforSEO request error: %s", e)
        raise HTTPException(status_code=502, detail=f"DataforSEO request failed: {e}")

    return JSONResponse(content=result)
