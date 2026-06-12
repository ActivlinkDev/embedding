import gzip
import json
import os
import sys
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dseo", tags=["Enrichment"])

mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["Activlink"]
dseo_results_collection = db["DSEO_Results"]
mastersku_collection = db["MasterSKU"]
locale_collection = db["Locale_Params"]

# Item types that wrap child items rather than carry pricing directly.
_CAROUSEL_TYPES = {
    "google_shopping_serp_carousel_element",
    "google_shopping_paid_carousel_element",
    "google_shopping_price_comparison_carousel_element",
}


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _flatten_items(items: list) -> list:
    """
    Expand carousel wrapper items into their children so that nested results
    (which carry the real seller/price/shopping_url fields) are searchable.
    Non-carousel items are kept as-is.
    """
    flat = []
    for item in items:
        if item.get("type") in _CAROUSEL_TYPES:
            nested = item.get("items") or []
            flat.extend(nested)
        else:
            flat.append(item)
    return flat


def _find_matching_item(items: list, model: str) -> dict | None:
    """Return the first item whose title contains the normalised model string.
    Returns None if model is empty — avoids enriching incomplete SKUs with an
    arbitrary first result (consistent with the previous ScaleSERP behaviour).
    """
    flat = _flatten_items(items)
    norm_model = _normalize(model)
    if not norm_model:
        return None
    for item in flat:
        if norm_model in _normalize(item.get("title") or ""):
            return item
    return None


def _process_task(task: dict) -> dict:
    """
    Extract the best-matching shopping item from a single DataforSEO task,
    resolve the locale, and upsert the relevant fields into MasterSKU
    Locale_Specific_Data.  Returns a status dict for logging.
    """
    task_data = task.get("data") or {}
    master_sku_id = task_data.get("tag")
    location_code = task_data.get("location_code")
    language_code = task_data.get("language_code", "en")

    if not master_sku_id:
        return {"status": "skipped", "reason": "no tag in task data"}

    # Resolve locale string from location_code
    locale_doc = locale_collection.find_one({"location_code": location_code}) if location_code else None
    locale = (locale_doc or {}).get("locale") or f"{language_code}_unknown"

    # Fetch MasterSKU
    try:
        ms_id = ObjectId(master_sku_id)
    except Exception:
        return {"status": "error", "reason": f"invalid tag ObjectId: {master_sku_id}"}

    ms_doc = mastersku_collection.find_one({"_id": ms_id})
    if not ms_doc:
        return {"status": "error", "reason": f"MasterSKU {master_sku_id} not found"}

    model = (ms_doc.get("Model") or "").strip()

    # Dig into result items
    results = task.get("result") or []
    items = (results[0].get("items") or []) if results else []
    if not items:
        return {"status": "no_results", "master_sku_id": master_sku_id, "locale": locale}

    item = _find_matching_item(items, model)
    if not item:
        return {"status": "no_match", "master_sku_id": master_sku_id, "locale": locale, "model": model}

    # Build the locale-specific update payload — field names match scale_lookup.py convention
    rating_obj = item.get("product_rating") or {}
    image_list = item.get("product_images") or []

    locale_update = {
        "SERP_Title": item.get("title"),
        "Google_ID": item.get("gid"),
        "Merchant": item.get("seller"),
        "Currency": item.get("currency"),
        "Price": item.get("price"),
        "Rating": rating_obj.get("value"),
        "Reviews": rating_obj.get("votes_count"),
        "Shopping_URL": item.get("shopping_url"),
        "Image": image_list[0] if image_list else None,
        "Product_ID": item.get("product_id"),
        "source": "DataforSEO",
        "serp_status": "found",
        "created_at": _utc_now_iso(),
    }

    # Upsert into Locale_Specific_Data array — same two-step pattern as scale_lookup.py
    result = mastersku_collection.update_one(
        {"_id": ms_id, "Locale_Specific_Data.locale": locale},
        {"$set": {f"Locale_Specific_Data.$.{k}": v for k, v in locale_update.items()}},
    )
    if result.matched_count == 0:
        mastersku_collection.update_one(
            {"_id": ms_id},
            {"$push": {"Locale_Specific_Data": {"locale": locale, **locale_update}}},
        )

    print(
        f"[DSEO Webhook] Updated MasterSKU {master_sku_id} locale={locale} "
        f"title={item.get('title')!r} price={item.get('price')} {item.get('currency')}",
        file=sys.stderr,
    )
    return {"status": "ok", "master_sku_id": master_sku_id, "locale": locale, "title": item.get("title")}


async def _parse_body(request: Request) -> dict:
    """
    Read the raw request body and JSON-decode it, decompressing gzip first
    when DataforSEO sends a Content-Encoding: gzip postback.
    """
    raw = await request.body()
    encoding = request.headers.get("content-encoding", "").lower()
    try:
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)
    except Exception as e:
        print(f"[DSEO Webhook] Failed to parse body (encoding={encoding!r}): {e}", file=sys.stderr)
        return {}


@router.post("/webhook")
async def dseo_webhook(request: Request):
    """
    Receives DataforSEO postback callbacks for merchant/google/products tasks.
    Handles gzip-compressed bodies, stores the raw payload, then maps the
    best-matching item into MasterSKU Locale_Specific_Data.
    Always returns 200 so DataforSEO does not retry.
    """
    task_id = request.query_params.get("id")

    body = await _parse_body(request)

    print(f"[DSEO Webhook] Received postback task_id={task_id}", file=sys.stderr)

    # Persist raw payload first — processing errors must not lose the raw data
    processing_results = []
    record = {
        "task_id": task_id,
        "received_at": _utc_now_iso(),
        "payload": body,
    }
    try:
        inserted = dseo_results_collection.insert_one(record)
        print(f"[DSEO Webhook] Stored raw result _id={inserted.inserted_id}", file=sys.stderr)
    except Exception as e:
        print(f"[DSEO Webhook] DB insert failed: {e}", file=sys.stderr)

    # Process each task in the response
    tasks = (body.get("tasks") or []) if isinstance(body, dict) else []
    for task in tasks:
        try:
            outcome = _process_task(task)
            processing_results.append(outcome)
        except Exception as e:
            print(f"[DSEO Webhook] Error processing task: {e}", file=sys.stderr)
            processing_results.append({"status": "error", "detail": str(e)})

    return JSONResponse(content={"status": "ok", "processed": processing_results}, status_code=200)
