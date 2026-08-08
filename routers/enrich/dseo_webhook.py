import gzip
import json
import os
import sys

from utils.api_docs import json_response
import traceback
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

from routers.sku.propagate_master_price import propagate_master_price

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
    return {
        "status": "ok",
        "master_sku_id": master_sku_id,
        "locale": locale,
        "title": item.get("title"),
        "product_id": item.get("product_id"),
        "price": item.get("price"),
        "currency": item.get("currency"),
    }


def _backfill_msrp_and_warm(master_sku_id, locale, price, currency):
    """Fill blank CustomSKU MSRPs from a freshly-enriched master price, then
    re-warm the widget quote cache for each SKU that changed.

    Scheduled as a background task rather than run inline: it scans and updates
    an unbounded number of CustomSKUs and then runs the full assignment + rating
    path per SKU. All of that is blocking, and the postback handler is an
    ``async def`` sharing a single Uvicorn worker's event loop with every other
    request. As a sync function, Starlette runs this in a threadpool.

    Never raises — both halves swallow their own errors.
    """
    targets = propagate_master_price(master_sku_id, locale, price, currency)
    if not targets:
        return
    # Imported lazily so a failure in the pricing import chain can't stop this
    # router from loading.
    from routers.widget_quote import warm_widget_cache

    for client_key, custom_sku_id, warm_locale in targets:
        warm_widget_cache(client_key, custom_sku_id, warm_locale)


def _process_product_info_task(task: dict) -> dict:
    """
    Handle a product_info postback: extract the product_info_element from
    result[0].items[0] and upsert it as an `extra_product_info` object into
    the matching MasterSKU Locale_Specific_Data entry.
    """
    task_data = task.get("data") or {}
    master_sku_id = task_data.get("tag")
    location_code = task_data.get("location_code")
    language_code = task_data.get("language_code", "en")

    if not master_sku_id:
        return {"status": "skipped", "reason": "no tag in task data"}

    locale_doc = locale_collection.find_one({"location_code": location_code}) if location_code else None
    locale = (locale_doc or {}).get("locale") or f"{language_code}_unknown"

    try:
        ms_id = ObjectId(master_sku_id)
    except Exception:
        return {"status": "error", "reason": f"invalid tag ObjectId: {master_sku_id}"}

    results = task.get("result") or []
    items = (results[0].get("items") or []) if results else []
    if not items:
        return {"status": "no_results", "master_sku_id": master_sku_id, "locale": locale}

    item = items[0]

    # Simplify sellers — keep only the fields needed for pricing display
    raw_sellers = item.get("sellers") or []
    sellers = [
        {
            "title": s.get("title"),
            "url": s.get("url"),
            "price": (s.get("price") or {}).get("current"),
            "currency": (s.get("price") or {}).get("currency"),
            "availability": s.get("product_availability"),
            "delivery": (s.get("delivery_info") or {}).get("delivery_message"),
        }
        for s in raw_sellers
    ]

    # Convert specifications list to {name: value} dict for easy lookup
    specs_dict = {
        s["specification_name"]: s.get("specification_value")
        for s in (item.get("specifications") or [])
        if s.get("specification_name")
    }

    extra_product_info = {
        "title": item.get("title"),
        "description": item.get("description"),
        "url": item.get("url"),
        "images": item.get("images") or [],
        "specifications": specs_dict,
        "sellers": sellers,
        "features": item.get("features"),
        "rating": item.get("rating"),
        "retrieved_at": _utc_now_iso(),
    }

    result = mastersku_collection.update_one(
        {"_id": ms_id, "Locale_Specific_Data.locale": locale},
        {"$set": {"Locale_Specific_Data.$.extra_product_info": extra_product_info}},
    )
    if result.matched_count == 0:
        mastersku_collection.update_one(
            {"_id": ms_id},
            {"$push": {"Locale_Specific_Data": {"locale": locale, "extra_product_info": extra_product_info}}},
        )

    print(
        f"[DSEO Webhook] Stored extra_product_info for MasterSKU {master_sku_id} locale={locale} "
        f"title={item.get('title')!r} sellers={len(sellers)} specs={len(specs_dict)}",
        file=sys.stderr,
    )
    return {
        "status": "ok",
        "master_sku_id": master_sku_id,
        "locale": locale,
        "title": item.get("title"),
        "sellers": len(sellers),
        "specs": len(specs_dict),
    }


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


def _slim_payload(body: dict) -> dict:
    """Return a trimmed copy of the body with items stripped from each result,
    using shallow copies to avoid the cost of deep-copying large payloads."""
    tasks_out = []
    for task in body.get("tasks") or []:
        results_out = []
        for res in task.get("result") or []:
            results_out.append({k: v for k, v in res.items() if k != "items"})
        tasks_out.append({**task, "result": results_out})
    return {**body, "tasks": tasks_out}


@router.post(
    "/webhook",
    summary="DataforSEO postback receiver (called by DataforSEO, not by you)",
    response_description="Acknowledgement, with a per-task account of what was processed.",
    responses={
        200: json_response(
            "**Always returned**, even when processing failed — see `processed` for what "
            "actually happened.",
            {
                "status": "ok",
                "processed": [
                    {
                        "function": "products",
                        "master_sku_id": "681aa2f1c4b21d0f8c9e0044",
                        "locale": "en_GB",
                        "status": "ok",
                        "product_id": "1234567890123456789",
                    }
                ],
            },
        ),
    },
)
async def dseo_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive completed enrichment tasks from DataforSEO. **DataforSEO calls this endpoint — you
    never do.**

    It handles the postbacks for both task types: `merchant/google/products` (prices) and
    `merchant/google/product_info` (details). Bodies may be gzip-compressed. A trimmed copy of
    every payload is stored for audit — the bulky `items` arrays are stripped — and the data is
    written onto the matching MasterSKU's locale record.

    **It always returns `200`**, even when a task cannot be matched or stored, so DataforSEO does
    not retry into a loop. The real outcome is in `processed`, one entry per task, each with its
    own `status`. Treat the status code as "received", never as "succeeded".

    When a shopping task yields a Google `Product_ID`, a `product_info` task is scheduled
    automatically — which is why one submission can produce two postbacks.

    There is **no bearer token** on this endpoint; the `id` query parameter is the task
    correlation, not a credential.
    """
    task_id = request.query_params.get("id")

    body = await _parse_body(request)

    print(f"[DSEO Webhook] Received postback task_id={task_id}", file=sys.stderr)

    processing_results = []

    record = {
        "task_id": task_id,
        "received_at": _utc_now_iso(),
        "payload": _slim_payload(body) if isinstance(body, dict) else body,
    }
    try:
        inserted = dseo_results_collection.insert_one(record)
        print(f"[DSEO Webhook] Stored result _id={inserted.inserted_id}", file=sys.stderr)
    except Exception as e:
        print(f"[DSEO Webhook] DB insert failed: {e}\n{traceback.format_exc()}", file=sys.stderr)

    # Dispatch each task to the correct handler based on the DataforSEO function type
    tasks = (body.get("tasks") or []) if isinstance(body, dict) else []
    for task in tasks:
        fn = (task.get("data") or {}).get("function", "")
        handler = _process_product_info_task if fn == "product_info" else _process_task
        try:
            outcome = handler(task)
        except Exception as e:
            print(f"[DSEO Webhook] Error processing task (function={fn!r}): {e}", file=sys.stderr)
            processing_results.append({"status": "error", "detail": str(e)})
            continue

        processing_results.append(outcome)

        # CustomSKUs created before this price landed were persisted with a
        # blank MSRP, which also left their widget quote cache cold. Backfill
        # and re-warm them off-request — see _backfill_msrp_and_warm.
        if fn != "product_info" and outcome.get("status") == "ok":
            background_tasks.add_task(
                _backfill_msrp_and_warm,
                outcome["master_sku_id"],
                outcome["locale"],
                outcome.get("price"),
                outcome.get("currency"),
            )

        # After a successful shopping task, auto-submit product_info if Product_ID was found
        if fn != "product_info" and outcome.get("status") == "ok" and outcome.get("product_id"):
            try:
                from routers.enrich.dseo_product_info import submit_dseo_product_info_task
            except Exception:
                from enrich.dseo_product_info import submit_dseo_product_info_task
            import asyncio
            asyncio.create_task(
                submit_dseo_product_info_task(
                    masterSKUid=outcome["master_sku_id"],
                    locale=outcome["locale"],
                )
            )
            print(
                f"[DSEO Webhook] Scheduled product_info task for MasterSKU {outcome['master_sku_id']} locale={outcome['locale']}",
                file=sys.stderr,
            )

    return JSONResponse(
        content={"status": "ok", "processed": processing_results},
        status_code=200,
        background=background_tasks,
    )
