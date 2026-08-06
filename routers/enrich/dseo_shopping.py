import os
import base64
import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

from utils.api_docs import error, json_response, secured
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
        raise ValueError("Missing DATAFORSEO_LOGIN or DATAFORSEO_PASSWORD")
    token = base64.b64encode(f"{DATAFORSEO_LOGIN}:{DATAFORSEO_PASSWORD}".encode()).decode()
    return f"Basic {token}"


async def submit_dseo_shopping_task(masterSKUid: str, locale: str) -> dict:
    """
    Core logic — submit a DataforSEO merchant/google/products task for the given
    MasterSKU.  Returns the raw DataforSEO JSON response dict.
    Raises ValueError for configuration/validation problems and httpx errors for
    network/API failures so callers can handle them appropriately.
    """
    if not DSEO_WEBHOOK_BASE_URL:
        raise ValueError("DSEO_WEBHOOK_BASE_URL is not configured")

    try:
        sku_doc = mastersku_collection.find_one({"_id": ObjectId(masterSKUid)})
    except Exception:
        raise ValueError(f"Invalid masterSKUid format: {masterSKUid!r}")

    if not sku_doc:
        raise ValueError(f"MasterSKU '{masterSKUid}' not found")

    make = (sku_doc.get("Make") or "").strip()
    model = (sku_doc.get("Model") or "").strip()
    if not model:
        raise ValueError(f"MasterSKU {masterSKUid} has no Model — cannot submit a reliable search task")
    keyword = f"{make} {model}".strip()

    locale_doc = locale_collection.find_one({"locale": locale})
    if not locale_doc:
        raise ValueError(f"Locale '{locale}' not found in Locale_Params — cannot resolve location_code or google_domain")

    google_domain = locale_doc.get("google_domain") or "google.co.uk"
    location_code = locale_doc.get("location_code") or 2826
    language_code = locale_doc.get("hl") or "en"

    postback_url = f"{DSEO_WEBHOOK_BASE_URL}/dseo/webhook?id=$id"

    payload = [
        {
            "language_code": language_code,
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

    result = response.json()
    logger.info("[dseo_shopping] Task submitted successfully tag=%s", masterSKUid)
    return result


@router.post(
    "/shopping",
    dependencies=[Depends(verify_token)],
    summary="Start a market price lookup for a MasterSKU",
    response_description="DataforSEO's task acknowledgement, passed through unchanged.",
    responses=secured({
        200: json_response(
            "The task was **accepted**, not completed. Prices arrive later via the webhook.",
            {
                "status_code": 20000,
                "status_message": "Ok.",
                "tasks": [
                    {
                        "id": "08061234-1234-0066-0000-a1b2c3d4e5f6",
                        "status_code": 20100,
                        "status_message": "Task Created.",
                        "data": {"keyword": "Bosch SMS6ZCI00G", "tag": "681aa2f1c4b21d0f8c9e0044"},
                    }
                ],
            },
        ),
        400: error("`masterSKUid` is malformed, or the MasterSKU has no Make/Model to search with.", "Invalid masterSKUid"),
        404: error("No MasterSKU with this id.", "MasterSKU not found"),
        502: error("DataforSEO could not be reached or rejected the request.", "DataforSEO error: 502"),
    }),
)
async def create_dseo_shopping_task(
    masterSKUid: str = Query(
        ...,
        description="**Mandatory.** The MasterSKU to price. Used both as the task tag and to build the search keyword from its Make and Model.",
        examples=["681aa2f1c4b21d0f8c9e0044"],
    ),
    locale: str = Query(
        "en_GB",
        description="Locale that decides which Google domain and location the prices come from.",
        examples=["en_GB"],
    ),
):
    """
    Start a Google Shopping price lookup for a MasterSKU.

    **This is asynchronous.** The endpoint submits a task to DataforSEO and returns their
    acknowledgement immediately — a `200` means the task was *accepted*, not that any price was
    found. The result is delivered later to `POST /dseo/webhook`, which writes the prices onto
    the MasterSKU's locale data. Poll the MasterSKU, not this endpoint.

    The search keyword is built from the MasterSKU's Make and Model, so a record missing those
    is rejected with `400`. `locale` chooses the Google domain and location, and therefore the
    market the prices reflect.

    When the shopping task finds a Google `Product_ID`, the webhook automatically follows up with
    a `product_info` task — so a single call here can produce two rounds of enrichment.

    The response body is **DataforSEO's own JSON**, forwarded unchanged.
    """
    try:
        result = await submit_dseo_shopping_task(masterSKUid, locale)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "Invalid masterSKUid" in msg or "no Make/Model" in msg:
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=500, detail=msg)
    except httpx.HTTPStatusError as e:
        logger.error("[dseo_shopping] DataforSEO HTTP error: %s", e)
        raise HTTPException(status_code=502, detail=f"DataforSEO error: {e.response.status_code}")
    except httpx.RequestError as e:
        logger.error("[dseo_shopping] DataforSEO request error: %s", e)
        raise HTTPException(status_code=502, detail=f"DataforSEO request failed: {e}")

    return JSONResponse(content=result)
