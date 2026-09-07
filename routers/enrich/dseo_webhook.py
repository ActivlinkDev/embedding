import gzip
import json
import os
import re
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
from utils.category_tree import min_market_price

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


def _title_words(title: str) -> list[str]:
    """A title split into normalised words, punctuation dropped.

    Kept separate from :func:`_normalize`, which folds the whole string into one
    run of alphanumerics. That folding is right for model numbers, which sellers
    punctuate freely ("DVN-04-X20W"), and wrong for brands — see
    :func:`_brand_present`.
    """
    return [word.lower() for word in re.split(r"[^0-9A-Za-z]+", title or "") if word]


def _brand_present(words: list[str], brand: str) -> bool:
    """Whether ``brand`` appears in ``words`` as whole words, not as a fragment.

    Substring matching cannot be used for a brand. Folded to bare alphanumerics,
    "GE" appears inside "fridge", "range" and "storage" — three of the commonest
    words in an appliance listing — so a GE SKU would match a Samsung fridge on
    any model-number coincidence. The model number is punctuated unpredictably
    and so still matches loosely; the brand is what has to be exact.

    Consecutive words are joined before comparing, so a make written as one word
    still matches a title that spaces it ("LG Electronics" -> "lgelectronics").
    """
    for start in range(len(words)):
        joined = ""
        for word in words[start:]:
            joined += word
            if joined == brand:
                return True
            if len(joined) >= len(brand):
                break
    return False


def _matching_items(items: list, make: str, model: str) -> list:
    """Every item whose title carries both the make and the model, in order.

    Returns nothing if either is empty — avoids enriching incomplete SKUs with
    an arbitrary first result (consistent with the previous ScaleSERP
    behaviour).

    The model alone is not enough to identify a product. ``_normalize`` strips
    titles to bare alphanumerics, so a model like ``DVN04X20W`` matches anywhere
    inside anything, including a listing for a different brand that happens to
    quote the code. Requiring the make narrows that: it is the one token a
    genuine listing for the product always carries.

    It is not sufficient on its own, though — a marketplace seller listing a
    spare part for six compatible Beko models passes both tests. The price floor
    in :func:`_implausible_price` is what catches those, and it is the guard that
    matters, because a wrong title displays badly while a wrong price rates the
    cover. Every match is returned rather than just the first so the caller can
    apply that floor to each in turn: a compatible accessory listed above the
    product itself must not decide the whole task.
    """
    brand = _brand_tokens(make)
    norm_model = _normalize(model)
    if not brand or not norm_model:
        return []
    matches = []
    for item in _flatten_items(items):
        title = item.get("title") or ""
        if norm_model not in _normalize(title):
            continue
        words = _title_words(title)
        if any(_brand_present(words, token) for token in brand):
            matches.append(item)
    return matches


def _find_matching_item(items: list, make: str, model: str) -> dict | None:
    """The first item matching both the make and the model, or None."""
    matches = _matching_items(items, make, model)
    return matches[0] if matches else None


def _brand_tokens(make: str) -> list[str]:
    """The forms of ``make`` a genuine listing title might carry.

    Icecat stores the legal entity — "LG Electronics", "Whirlpool Corporation" —
    while a shopping listing prints the brand alone. Requiring the full string
    would turn working enrichments into no-matches, so the first word counts as
    well. Only those two forms: matching on any word would let "Electronics" or
    "Corporation" stand in for the brand and pass nearly every title.

    Two characters is the floor, which keeps real short brands (LG, GE) while
    rejecting a single stray letter. Those are safe here only because
    :func:`_brand_present` compares whole words; as a substring "GE" matches
    half the appliance listings on the page.
    """
    full = _normalize(make)
    if not full:
        return []
    first = _normalize((make.strip().split() or [""])[0])
    if first and first != full and len(first) >= 2:
        return [full, first]
    return [full]


def _implausible_price(price, currency: str | None, category: str | None) -> str | None:
    """Why ``price`` cannot be a real price for ``category``, or None if it can.

    The floor comes from the category taxonomy and is usually absent, in which
    case any price passes and behaviour is unchanged. Where one is configured it
    is the last line of defence against a mis-matched SERP item: enrichment
    matches on the product name, and an accessory or spare-part listing that
    quotes the model number is indistinguishable from the appliance by title
    alone. ``market.referencePrice`` feeds the quote, so storing 11.99 for a
    dishwasher under-rates the cover rather than merely looking wrong.

    A missing or non-numeric price is not judged here — callers already drop
    those, and reporting them as implausible would blame the wrong thing.
    """
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    floor = min_market_price(category, currency)
    if floor is None or price >= floor:
        return None
    return (
        f"price {price} {currency or ''}".strip()
        + f" is below the {floor} floor for category {category!r}"
    )


def _price_stats(sellers: list, preferred_currency: str | None) -> dict | None:
    """
    Summarise the seller offers into min/mean, and name the cheapest merchant,
    for a single currency.

    ``preferred_currency`` is the currency already recorded on the locale (set by
    the shopping task). Anchoring on it keeps ``referencePrice`` and
    ``market.currency`` describing the same money — a seller list can carry
    offers from more than one currency, and averaging across them would be
    meaningless. When the locale has no currency yet, the most common currency
    in the list wins.

    Returns None when no seller carries a usable price, so callers can leave the
    existing reference price alone rather than blanking it.
    """
    priced: list[tuple[str, float, str]] = []
    for seller in sellers:
        value = seller.get("price")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value <= 0:
            continue
        currency = (seller.get("currency") or "").strip().upper()
        priced.append((currency, float(value), (seller.get("title") or "").strip()))

    if not priced:
        return None

    target = (preferred_currency or "").strip().upper()
    offers = [o for o in priced if o[0] == target] if target else []
    if not offers:
        # No offers in the locale's currency (or none recorded yet) — fall back to
        # whichever currency the sellers mostly quote, and report it so the caller
        # can keep market.currency in step.
        counts: dict[str, int] = {}
        for currency, _, _title in priced:
            counts[currency] = counts.get(currency, 0) + 1
        target = max(counts, key=lambda c: (counts[c], c))
        offers = [o for o in priced if o[0] == target]

    prices = [price for _c, price, _t in offers]
    # min() over the tuples would tie-break on the seller name; DataforSEO returns
    # the sellers in Google's own order, so the first offer at the lowest price is
    # the one to name.
    cheapest = min(offers, key=lambda o: o[1])
    return {
        "currency": target,
        "min": cheapest[1],
        "mean": round(sum(prices) / len(prices), 2),
        "count": len(prices),
        "merchant": cheapest[2] or None,
    }


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

    identifiers = ms_doc.get("identifiers") or {}
    make = (identifiers.get("make") or "").strip()
    model = (identifiers.get("model") or "").strip()
    category = (ms_doc.get("category") or "").strip()

    # Dig into result items
    results = task.get("result") or []
    items = (results[0].get("items") or []) if results else []
    if not items:
        return {"status": "no_results", "master_sku_id": master_sku_id, "locale": locale}

    candidates = _matching_items(items, make, model)
    if not candidates:
        return {
            "status": "no_match",
            "master_sku_id": master_sku_id,
            "locale": locale,
            "make": make,
            "model": model,
        }

    # A price the category cannot plausibly carry means the title matched
    # something that is not the product — an accessory or a spare part quoting
    # the model number. Such a candidate is skipped rather than ending the task:
    # Google orders the results, and a compatible accessory listed above the
    # appliance would otherwise reject a page that also holds the real product.
    # Only when every match fails is the task rejected, reported against the
    # first — and then the whole item is dropped, not just its price, because
    # the rest of it describes that same wrong listing and its product_id would
    # send the follow-up product_info round after it too.
    item = None
    rejection = None
    for candidate in candidates:
        reason = _implausible_price(
            candidate.get("price"), candidate.get("currency"), category
        )
        if reason is None:
            item = candidate
            break
        if rejection is None:
            rejection = reason
    if item is None:
        now = utc_now()
        mastersku_collection.update_one(
            {"_id": ms_id},
            {"$set": {
                f"locales.{locale}.enrichment.source": "DataforSEO",
                f"locales.{locale}.enrichment.status": "rejected",
                f"locales.{locale}.enrichment.rejectedReason": rejection,
                f"locales.{locale}.enrichment.updatedAt": now,
            }},
        )
        print(
            f"[DSEO Webhook] Rejected all {len(candidates)} match(es) for MasterSKU "
            f"{master_sku_id} locale={locale} "
            f"title={candidates[0].get('title')!r}: {rejection}",
            file=sys.stderr,
        )
        return {
            "status": "rejected",
            "master_sku_id": master_sku_id,
            "locale": locale,
            "reason": rejection,
            "title": candidates[0].get("title"),
            "candidates": len(candidates),
        }

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

    # The title is deliberately not taken from the SERP item. This task's job is
    # pricing; the title it carries is a merchant's listing name, written to win
    # a search rather than to name a product, and overwriting the Icecat copy
    # seeded at creation with it produced entries like "Beko Din15c20 Dvn04x20w
    # Din15x20 Dvn04x20s Bdfn15420 Dvs04x20x". It is only used to fill a locale
    # that has no title at all, where anything beats blank.
    existing_title = (
        ((ms_doc.get("locales") or {}).get(locale) or {}).get("title") or ""
    ).strip()
    serp_title = (item.get("title") or "").strip()
    if not existing_title and serp_title:
        locale_update[f"{prefix}.title"] = serp_title

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

    The seller list this carries also re-prices the SKU: referencePrice becomes
    the cheapest offer and merchant the seller quoting it, with
    priceMin/priceMean/priceSampleSize stored beside them.
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
    prefix = f"locales.{locale}"

    # The shopping task set referencePrice from the price printed on the Google
    # Shopping tile, which is a single headline offer and is regularly well above
    # what the product actually sells for. The seller list is the better source:
    # take the cheapest offer as the reference price and keep the mean alongside
    # it so the spread stays visible. referencePrice feeds the quote
    # (services/catalog.py resolves it into product.price), so an inflated one
    # rates the customer's cover too high.
    ms_doc = mastersku_collection.find_one(
        {"_id": ms_id}, {f"{prefix}.market.currency": 1, "category": 1}
    ) or {}
    existing_currency = (
        ((ms_doc.get("locales") or {}).get(locale) or {}).get("market") or {}
    ).get("currency")
    category = (ms_doc.get("category") or "").strip()
    stats = _price_stats(sellers, existing_currency)

    # The cheapest seller is where a mis-match shows up first: a seller list for
    # the wrong product is cheapest exactly where it is least like the appliance.
    # A price the category cannot carry condemns the whole element, not just the
    # price — the description, features, specs and gallery below all describe
    # that same listing, and writing them would overwrite good Icecat copy with
    # an accessory's. Record why and leave the locale as it stands.
    rejection = (
        _implausible_price(stats["min"], stats["currency"], category) if stats else None
    )
    if rejection:
        mastersku_collection.update_one(
            {"_id": ms_id},
            {"$set": {
                f"{prefix}.enrichment.productInfoAt": now,
                f"{prefix}.enrichment.rejectedReason": rejection,
            }},
        )
        print(
            f"[DSEO Webhook] Rejected product_info for MasterSKU {master_sku_id} "
            f"locale={locale} title={item.get('title')!r}: {rejection}",
            file=sys.stderr,
        )
        return {
            "status": "rejected",
            "master_sku_id": master_sku_id,
            "locale": locale,
            "reason": rejection,
            "title": item.get("title"),
        }

    update = {
        f"{prefix}.market.sellers": sellers,
        f"{prefix}.enrichment.productInfoAt": now,
        f"{prefix}.updatedAt": now,
        "updatedAt": now,
    }
    if stats:
        update[f"{prefix}.market.referencePrice"] = stats["min"]
        update[f"{prefix}.market.priceMin"] = stats["min"]
        update[f"{prefix}.market.priceMean"] = stats["mean"]
        update[f"{prefix}.market.priceSampleSize"] = stats["count"]
        update[f"{prefix}.market.currency"] = stats["currency"]
        # merchant names whoever quotes referencePrice. Leave the shopping task's
        # value in place when the cheapest offer is anonymous, rather than
        # replacing a real name with nothing.
        if stats["merchant"]:
            update[f"{prefix}.market.merchant"] = stats["merchant"]

    # These four are populated from Icecat at creation. DataforSEO frequently returns a
    # product_info element with some of them missing, so only overwrite what it actually
    # carries — an unconditional $set would blank good catalogue copy.
    for path, value in (
        (f"{prefix}.description", extra_product_info.get("description")),
        (f"{prefix}.features", extra_product_info.get("features")),
        (f"{prefix}.specifications", specs_dict),
        (f"{prefix}.assets.gallery", extra_product_info.get("images")),
    ):
        if value:
            update[path] = value
    mastersku_collection.update_one({"_id": ms_id}, {"$set": update})

    print(
        f"[DSEO Webhook] Stored extra_product_info for MasterSKU {master_sku_id} locale={locale} "
        f"title={item.get('title')!r} sellers={len(sellers)} specs={len(specs_dict)} "
        f"priceMin={(stats or {}).get('min')} priceMean={(stats or {}).get('mean')}",
        file=sys.stderr,
    )
    return {
        "status": "ok",
        "master_sku_id": master_sku_id,
        "locale": locale,
        "title": item.get("title"),
        "sellers": len(sellers),
        "specs": len(specs_dict),
        "price_min": (stats or {}).get("min"),
        "price_mean": (stats or {}).get("mean"),
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
                    },
                    {
                        "function": "products",
                        "master_sku_id": "681aa2f1c4b21d0f8c9e0055",
                        "locale": "en_GB",
                        "status": "rejected",
                        "reason": "price 11.99 GBP is below the 80.0 floor for category 'Dishwasher'",
                    },
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

    A task whose best match carries a price the SKU's category cannot plausibly have — an
    accessory or spare-part listing that quotes the model number — is reported as `rejected` and
    nothing from it is stored, because every other field describes that same wrong listing. The
    floor comes from `min_market_price` on the `Category` document and categories without one are
    never rejected. `locales.<locale>.title` is only ever filled in when the locale has no title:
    the catalogue title written at creation is the product's name, while the one on a shopping
    result is a merchant's listing copy.

    When a shopping task yields a Google `Product_ID`, a `product_info` task is scheduled
    automatically — which is why one submission can produce two postbacks. The two rounds price
    the SKU differently on purpose: the shopping task records the price on the Google Shopping
    tile, and the `product_info` round then replaces `market.referencePrice` and
    `market.merchant` with the cheapest seller it found, recording `priceMin`, `priceMean` and
    `priceSampleSize` alongside them.

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
        # derived quote caches; no catalogue data is copied. Both task types can
        # move the reference price — the shopping task sets it from the SERP tile,
        # the product_info task replaces it with the cheapest seller — so a
        # product_info postback that produced a price has to re-warm too, or the
        # caches keep quoting the superseded figure.
        price_changed = outcome.get("status") == "ok" and (
            fn != "product_info" or outcome.get("price_min") is not None
        )
        if price_changed:
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
