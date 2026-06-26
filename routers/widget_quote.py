"""Embeddable widget quote endpoints.

Two routes power the Shopify-style protection widget:

* ``POST /widget_price`` — display only. Returns priced protection options for a
  CustomSKU + price, served from ``WidgetQuoteCache`` where possible. **No quote
  is persisted** (no Quotes doc, no quote_id) just because the sidebar is shown.

* ``POST /widget_quote`` — commit. Called only when the shopper clicks
  "Add with Protection". Persists a ``Quotes`` doc for the chosen option and
  returns ``{ quote_id }``. This is the only place a quote id is created.

Both reuse the existing assignment (``product_assignment``) and rating
(``price_and_group``) logic — no new pricing code. A pre-purchase widget has no
registered device, so this path starts from a CustomSKU and never writes a
``Devices`` doc.
"""

from fastapi import APIRouter, HTTPException, Depends, Request, BackgroundTasks
from pydantic import BaseModel, Field
from typing import List, Optional
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import os

from utils.dependencies import verify_token
from .product_assignment import (
    product_assignment,
    ProductAssignmentRequest,
    calculate_age_in_months,
)
from .rate_request import RateRequest as RateReqModel, price_and_group, store_quote

router = APIRouter(tags=["Quotes"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
clientkey_collection = db["ClientKey"]
customsku_collection = db["CustomSKU"]
locale_params_collection = db["Locale_Params"]
widget_cache_collection = db["WidgetQuoteCache"]

CACHE_TTL = timedelta(days=7)


# ---------- Models ----------

class WidgetPriceRequest(BaseModel):
    clientKey: str = Field(..., example="AOPON12345")
    customSkuId: str = Field(..., example="64f7a1e4b9c1f2a3d4e5f6a7")
    price: float = Field(..., example=999.99)
    locale: str = Field(..., example="en_GB")
    currency: Optional[str] = Field(None, example="GBP")
    purchaseDate: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to today (new purchase)")
    gtee: Optional[int] = Field(None, description="Guarantee duration override (months)")


class WidgetQuoteRequest(WidgetPriceRequest):
    productId: str = Field(..., description="Chosen product id")
    optionId: Optional[str] = Field(None, description="Chosen option identifier (e.g. poc)")


# ---------- Helpers ----------

def _to_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _lsd_for_locale(customsku_doc, locale):
    for entry in customsku_doc.get("Locale_Specific_Data", []) or []:
        if entry.get("locale") == locale:
            return entry
    return {}


def resolve_widget_inputs(payload: WidgetPriceRequest):
    """Resolve a widget request into a ProductAssignmentRequest + context.

    Returns ``(assignment_request, age_in_months, client_doc, customsku_doc, lsd)``.
    Raises HTTPException on bad client / sku / locale.
    """
    client_doc = clientkey_collection.find_one({"ClientKey": payload.clientKey})
    if not client_doc or "Client_ID" not in client_doc:
        raise HTTPException(status_code=404, detail="Invalid clientKey")
    client_id = client_doc["Client_ID"]
    source = (client_doc.get("Source") or "").strip()

    try:
        sku_object_id = ObjectId(payload.customSkuId)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid customSkuId")

    customsku_doc = customsku_collection.find_one({
        "_id": sku_object_id,
        "Client": client_id,
        "Locale_Specific_Data.locale": payload.locale,
    })
    if not customsku_doc:
        raise HTTPException(status_code=404, detail="CustomSKU not found for client/locale")

    lsd = _lsd_for_locale(customsku_doc, payload.locale)

    # Category — from the CustomSKU root (mirrors the registration/assignment flow)
    category = (customsku_doc.get("Category") or lsd.get("Locale_Matched_Category") or "").strip()

    # Currency — explicit > locale-specific > Locale_Params
    currency = (payload.currency or lsd.get("Currency") or "").strip().upper()
    if not currency:
        locale_doc = locale_params_collection.find_one({"locale": payload.locale}) or {}
        currency = (locale_doc.get("currency") or "").strip().upper()

    # Guarantee duration — override > Labour > Parts
    if payload.gtee is not None:
        gtee = _to_int(payload.gtee)
    else:
        guarantees = lsd.get("Guarantees", {}) or {}
        gtee = _to_int(guarantees.get("Labour")) or _to_int(guarantees.get("Parts"))

    # Price — request value, fall back to MSRP
    price = payload.price or _to_int(lsd.get("MSRP"))

    # Purchase date — request value, else today (new purchase)
    purchase_date = (payload.purchaseDate or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")

    assignment_request = ProductAssignmentRequest(
        client=client_id,
        source=source,
        category=category,
        price=price,
        locale=payload.locale,
        purchase_date=purchase_date,
        gtee=gtee,
        currency=currency,
    )
    age_in_months = calculate_age_in_months(purchase_date)
    return assignment_request, age_in_months, client_doc, customsku_doc, lsd


def compute_options(payload: WidgetPriceRequest):
    """Run assignment + rating for a widget request. Returns (grouped, bracket, currency)."""
    assignment_request, age_in_months, _client_doc, _sku, _lsd = resolve_widget_inputs(payload)

    assignment_result = product_assignment(assignment_request)
    products = assignment_result.get("products") or []
    if not products:
        return [], None, assignment_request.currency

    requests: List[RateReqModel] = []
    for prod in products:
        product_id = prod["productId"]
        poc = prod.get("POC", {})
        mode = poc.get("mode")
        for duration in poc.get("durationMonths", []):
            requests.append(RateReqModel(
                product_id=product_id,
                currency=assignment_request.currency,
                locale=assignment_request.locale,
                poc=int(duration),
                category=assignment_request.category,
                age=age_in_months,
                price=assignment_request.price,
                multi_count=1,
                client=assignment_request.client,
                source=assignment_request.source,
                mode=mode or "live",
            ))

    grouped, bracket = price_and_group(requests)
    return grouped, bracket, assignment_request.currency


# ---------- Cache ----------

def _cache_read(custom_sku_id, locale, age, price):
    doc = widget_cache_collection.find_one({
        "customSkuId": custom_sku_id,
        "locale": locale,
        "age": age,
        "priceLow": {"$lte": price},
        "priceHigh": {"$gte": price},
    })
    if not doc:
        return None
    generated_at = doc.get("generatedAt")
    if not generated_at or (datetime.utcnow() - generated_at) > CACHE_TTL:
        return None
    return doc


def _cache_invalidate(custom_sku_id, locale=None):
    query = {"customSkuId": custom_sku_id}
    if locale is not None:
        query["locale"] = locale
    widget_cache_collection.delete_many(query)


def _cache_write(custom_sku_id, locale, age, price, bracket, currency, grouped):
    low, high = bracket if bracket else (price, price)
    widget_cache_collection.update_one(
        {"customSkuId": custom_sku_id, "locale": locale, "age": age, "priceLow": low, "priceHigh": high},
        {"$set": {
            "customSkuId": custom_sku_id,
            "locale": locale,
            "age": age,
            "priceLow": low,
            "priceHigh": high,
            "currency": currency,
            "options": grouped,
            "generatedAt": datetime.utcnow(),
        }},
        upsert=True,
    )


def _priced_options(payload: WidgetPriceRequest):
    """Return (grouped, currency), using the cache when fresh."""
    # Resolve age cheaply for the cache key (purchase_date defaults to today)
    purchase_date = (payload.purchaseDate or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")
    age = calculate_age_in_months(purchase_date)

    cached = _cache_read(payload.customSkuId, payload.locale, age, payload.price)
    if cached:
        return cached.get("options", []), cached.get("currency")

    grouped, bracket, currency = compute_options(payload)
    if grouped:
        _cache_write(payload.customSkuId, payload.locale, age, payload.price, bracket, currency, grouped)
    return grouped, currency


# ---------- Origin allowlist ----------

def _enforce_origin(request: Request, client_doc):
    """If the client restricts domains and an Origin is present, enforce it.

    The primary enforcement is in the Next.js proxy (which also sets CORS); this
    is defence-in-depth for the rare case the embedding service is reached
    directly with a browser Origin header.
    """
    if client_doc is None:
        return
    if client_doc.get("widget_enabled") is False:
        raise HTTPException(status_code=403, detail="Widget disabled for client")
    allowed = client_doc.get("allowed_domains") or []
    origin = request.headers.get("origin")
    if origin and allowed and origin not in allowed:
        raise HTTPException(status_code=403, detail="Origin not allowed")


# ---------- Endpoints ----------

@router.post("/widget_price")
def widget_price(payload: WidgetPriceRequest, request: Request, _: None = Depends(verify_token)):
    """Display-only: priced options for the drawer. No quote is persisted."""
    client_doc = clientkey_collection.find_one({"ClientKey": payload.clientKey})
    _enforce_origin(request, client_doc)

    grouped, currency = _priced_options(payload)
    if not grouped:
        raise HTTPException(status_code=404, detail="No protection options available for this product")

    return {
        "currency": currency,
        "mode": (client_doc or {}).get("widget_mode", "redirect"),
        "options": grouped,
    }


@router.post("/widget_quote")
def widget_quote(payload: WidgetQuoteRequest, request: Request, _: None = Depends(verify_token)):
    """Commit: create the quote when the shopper proceeds. Returns quote_id."""
    client_doc = clientkey_collection.find_one({"ClientKey": payload.clientKey})
    _enforce_origin(request, client_doc)

    grouped, currency = _priced_options(payload)
    if not grouped:
        raise HTTPException(status_code=404, detail="No protection options available for this product")

    quote_id = store_quote(
        grouped,
        device_id=None,
        client_key=payload.clientKey,
        extra={
            "customSkuId": payload.customSkuId,
            "locale": payload.locale,
            "currency": currency,
            "source": "widget",
            "selected": {"productId": payload.productId, "optionId": payload.optionId},
        },
    )
    return {"quote_id": quote_id}


@router.post("/widget_quote/refresh")
def widget_quote_refresh(payload: WidgetPriceRequest, _: None = Depends(verify_token)):
    """Admin: force-rebuild the cache entry for a CustomSKU + locale + price."""
    grouped, bracket, currency = compute_options(payload)
    if not grouped:
        raise HTTPException(status_code=404, detail="No protection options to cache")
    purchase_date = (payload.purchaseDate or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")
    age = calculate_age_in_months(purchase_date)
    _cache_write(payload.customSkuId, payload.locale, age, payload.price, bracket, currency, grouped)
    return {"status": "ok", "cached_options": len(grouped)}


# ---------- Warm-on-write (importable) ----------

def warm_widget_cache(client_key: str, custom_sku_id: str, locale: str, price: Optional[float] = None):
    """Precompute and cache the widget quote for a CustomSKU at MSRP.

    Safe to call from a background task on CustomSKU create/update. Never raises —
    failures are logged and swallowed so they can't break the SKU write.
    """
    try:
        if price is None:
            sku = customsku_collection.find_one({"_id": ObjectId(custom_sku_id)})
            lsd = _lsd_for_locale(sku, locale) if sku else {}
            price = float(lsd.get("MSRP") or 0)
        if not price:
            return
        payload = WidgetPriceRequest(
            clientKey=client_key, customSkuId=custom_sku_id, price=price, locale=locale
        )
        grouped, bracket, currency = compute_options(payload)
        if grouped:
            _cache_invalidate(custom_sku_id, locale)
            age = calculate_age_in_months(datetime.utcnow().strftime("%Y-%m-%d"))
            _cache_write(custom_sku_id, locale, age, price, bracket, currency, grouped)
            print(f"[WIDGET-CACHE] warmed {custom_sku_id} / {locale}")
    except Exception as e:
        print(f"[WIDGET-CACHE] warm failed for {custom_sku_id}/{locale}: {e}")
