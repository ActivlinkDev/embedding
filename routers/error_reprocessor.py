# error_reprocessor.py
# Background worker to reprocess Custom SKU lookup errors safely and concurrently.
# - Atomic job-claiming prevents double-processing
# - Bounded retries with terminal status
# - Configurable via environment variables
# - Calls your existing create_custom_sku business logic
#
# Env vars (with defaults):
#   MONGO_URI=mongodb://localhost:27017
#   MONGO_DB=Activlink
#   ERROR_COLLECTION=Error_Log_Lookup_Custom_SKU
#   ERROR_REPROCESSOR_POLL_SECONDS=60
#   ERROR_REPROCESSOR_MAX_RETRIES=5
#   ERROR_REPROCESSOR_CONCURRENCY=4
#   LOG_LEVEL=INFO
#
# NOTE:
# If `create_custom_sku` is a FastAPI route handler, prefer exposing an async
# *service-layer* function (e.g., create_custom_sku_service) and import that instead.
# This file assumes create_custom_sku(request, ctx) returns a serializable result.

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

# Import your request models + function. Adjust the path if your project structure differs.
# Recommended: from routers.sku.create_custom_sku import create_custom_sku_service as create_custom_sku
from routers.sku.create_custom_sku import (
    create_custom_sku,          # replace with create_custom_sku_service if available
    CustomSKURequest,
    LocaleDetails,
    CustomLink,
)

load_dotenv()

# --- Config ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB", "Activlink")
ERROR_COLLECTION = os.getenv("ERROR_COLLECTION", "Error_Log_Lookup_Custom_SKU")
POLL_SECONDS = int(os.getenv("ERROR_REPROCESSOR_POLL_SECONDS", "60"))
MAX_RETRIES = int(os.getenv("ERROR_REPROCESSOR_MAX_RETRIES", "5"))
CONCURRENCY = int(os.getenv("ERROR_REPROCESSOR_CONCURRENCY", "4"))

# --- Logging ---
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("error_reprocessor")

# --- Mongo ---
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]
error_log_collection = db[ERROR_COLLECTION]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _build_request_from_payload(payload: dict) -> CustomSKURequest:
    """
    Transform an Error_Log payload into a CustomSKURequest.
    Ensures list fields are lists (not None) and supplies safe defaults.
    """
    locale_code = payload.get("locale", "")
    locale_detail_data = payload.get("Locale_Details", {}) or {}

    links_src = locale_detail_data.get("Custom_Links") or []
    custom_links = [CustomLink(**cl) for cl in links_src] if links_src else []

    locale_details = LocaleDetails(
        Title=locale_detail_data.get("Title", ""),
        Price=locale_detail_data.get("Price", 0),
        GTL=locale_detail_data.get("GTL", 0),
        GTP=locale_detail_data.get("GTP", 0),
        Promo_Code=locale_detail_data.get("Promo_Code", ""),
        Custom_Links=custom_links,  # ensure list, not None
    )

    transformed_payload = {
        "ClientKey": payload.get("clientKey"),
        "Locale": locale_code,
        "SKU": payload.get("SKU"),
        "Source": payload.get("source", "API_Reprocessor"),
        "GTIN": payload.get("GTIN", ""),
        "Make": payload.get("Make", ""),
        "Model": payload.get("Model", ""),
        "Category": payload.get("Category", ""),
        "Locale_Details": locale_details,
    }

    return CustomSKURequest(**transformed_payload)


async def _claim_one_job() -> Optional[dict]:
    """
    Atomically claim the next error job (status: 'error', retry_count < MAX_RETRIES).
    Marks it as 'processing' with timestamps to avoid duplicate workers.
    """
    return await error_log_collection.find_one_and_update(
        {
            "status": "error",
            "$or": [
                {"retry_count": {"$lt": MAX_RETRIES}},
                {"retry_count": {"$exists": False}},
            ],
        },
        {
            "$set": {"status": "processing", "processing_started_at": _utcnow()},
            "$inc": {"retry_count": 1},
        },
        sort=[("_id", 1)],
        return_document=ReturnDocument.AFTER,
    )


async def _process_job(doc: dict):
    """
    Execute the reprocessing for a single claimed job doc.
    On success: status -> 'reprocessed' with result payload.
    On failure: status -> 'error' (if will retry) or 'reprocess_failed' (terminal).
    """
    doc_id = doc["_id"]
    payload = doc.get("payload", {}) or {}
    retry_count = doc.get("retry_count", 1)

    try:
        request = _build_request_from_payload(payload)

        # Prefer calling an async service function if available:
        # result = await create_custom_sku_service(request)
        if asyncio.iscoroutinefunction(create_custom_sku):
            result = await create_custom_sku(request, None)
        else:
            # If create_custom_sku is synchronous, call directly.
            # If it blocks on I/O, consider running it in a thread executor.
            result = create_custom_sku(request, None)

        await error_log_collection.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "status": "reprocessed",
                    "reprocessed_at": _utcnow(),
                    "result": result,
                },
                "$unset": {"processing_started_at": ""},
            },
        )
        log.info("✅ Reprocessed %s (retries=%s)", doc_id, retry_count)

    except Exception as e:
        status = "reprocess_failed" if retry_count >= MAX_RETRIES else "error"
        await error_log_collection.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "status": status,
                    "error": str(e),
                    "last_attempted_at": _utcnow(),
                },
                "$unset": {"processing_started_at": ""},
            },
        )
        log.error("❌ Failed %s (retries=%s): %s", doc_id, retry_count, e)


async def _worker_loop(sema: asyncio.Semaphore):
    """
    Poll → claim → process. Sleeps only when there is no work.
    """
    while True:
        doc = await _claim_one_job()
        if not doc:
            await asyncio.sleep(POLL_SECONDS)
            continue

        # Limit concurrent in-flight jobs across workers
        async with sema:
            await _process_job(doc)


async def main():
    log.info(
        "⏳ Starting error reprocessor | poll=%ss max_retries=%s concurrency=%s",
        POLL_SECONDS, MAX_RETRIES, CONCURRENCY
    )
    sema = asyncio.Semaphore(CONCURRENCY)
    tasks = [asyncio.create_task(_worker_loop(sema)) for _ in range(CONCURRENCY)]
    try:
        await asyncio.gather(*tasks)
    finally:
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
