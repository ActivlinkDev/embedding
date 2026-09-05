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

from services.catalog import utc_now

load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dseo", tags=["Enrichment"])

mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["Activlink"]
dseo_results_collection = db["DSEO_Results"]
mastersku_collection = db["MasterSKU"]
customsku_collection = db["CustomSKU"]
clientkey_collection = db["ClientKey"]
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
    locale map. Returns a status dict for logging.
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

    model = ((ms_doc.get("identifiers") or {}).get("model") or "").strip()

    # Dig into result items
    results = task.get("result") or []
    items = (results[0].get("items") or []) if results else []
    if not items:
        return {"status": "no_results", "master_sku_id": master_sku_id, "locale": locale}

    item = _find_matching_item(items, model)
    if not item:
        return {"status": "no_match", "master_sku_id": master_sku_id, "locale": locale, "model": model}

    # Build the canonical locale market update.
    rating_obj = item.get("product_rating") or {}
    if not isinstance(rating_obj, dict):
        rating_obj = {}
    image_list = item.get("product_images") or []
    if not isinstance(image_list, list):
        image_list = []

    now = utc_now()
    prefix = f"locales.{locale}"
    locale_update = {
        f"{prefix}.enrichment.source": "DataforSEO",
        f"{prefix}.enrichment.status": "found",
        f"{prefix}.enrichment.updatedAt": now,
        f"{prefix}.updatedAt": now,
        "updatedAt": now,
    }
    optional_values = {
        f"{prefix}.title": item.get("title"),
        f"{prefix}.market.googleId": item.get("gid"),
        f"{prefix}.market.merchant": item.get("seller"),
        f"{prefix}.market.currency": item.get("currency"),
        f"{prefix}.market.referencePrice": item.get("price"),
        f"{prefix}.market.rating": rating_obj.get("value"),
        f"{prefix}.market.reviews": rating_obj.get("votes_count"),
        f"{prefix}.market.shoppingUrl": item.get("shopping_url"),
        f"{prefix}.market.productId": item.get("product_id"),
    }
    locale_update.update({
        path: value
        for path, value in optional_values.items()
        if value is not None and (not isinstance(value, str) or value.strip())
    })
    if image_list and image_list[0]:
        locale_update[f"{prefix}.assets.primaryImage"] = image_list[0]
    mastersku_collection.update_one({"_id": ms_id}, {"$set": locale_update})

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


def _warm_inherited_skus(master_sku_id, locale):
    """Rebuild quote caches for SKUs inheriting this master's locale price."""
    try:
        master_id = ObjectId(master_sku_id)
    except Exception:
        return
    from routers.widget_quote import warm_widget_cache
    query = {
        "masterSkuId": master_id,
        "enabledLocales": locale,
        f"overrides.locales.{locale}.price": {"$exists": False},
    }
    for custom in customsku_collection.find(query, {"clientId": 1}):
        client = clientkey_collection.find_one({"Client_ID": custom.get("clientId")}, {"ClientKey": 1})
        if client and client.get("ClientKey"):
            warm_widget_cache(client["ClientKey"], str(custom["_id"]), locale)


def _process_product_info_task(task: dict) -> dict:
    """
    Handle a product_info postback: extract the product_info_element from
    result[0].items[0] and upsert it as an `extra_product_info` object into
    the matching MasterSKU locale entry.
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

    now = utc_now()
    mastersku_collection.update_one(
        {"_id": ms_id},
        {"$set": {
            f"locales.{locale}.description": extra_product_info.get("description"),
            f"locales.{locale}.features": extra_product_info.get("features"),
            f"locales.{locale}.specifications": specs_dict,
            f"locales.{locale}.assets.gallery": extra_product_info.get("images") or [],
            f"locales.{locale}.market.sellers": sellers,
            f"locales.{locale}.enrichment.productInfoAt": now,
            f"locales.{locale}.updatedAt": now,
            "updatedAt": now,
        }},
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

        # CustomSKUs inherit master pricing at read time. Rebuild only their
        # derived quote caches; no catalogue data is copied.
        if fn != "product_info" and outcome.get("status") == "ok":
            background_tasks.add_task(
                _warm_inherited_skus,
                outcome["master_sku_id"],
                outcome["locale"],
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
