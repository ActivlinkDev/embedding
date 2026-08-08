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

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from typing import List, Optional
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import os

from utils.api_docs import error, json_response, secured
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
    """A product in a storefront basket, priced for the widget.

    The device does not need to be registered: the widget prices straight from the catalogue,
    so `customSkuId` plus `locale` stands in for a device record.
    """

    clientKey: str = Field(
        ...,
        description="**Mandatory.** Tenant key. Also decides whether the widget is enabled and which origins may call it.",
        examples=["acme_uk_live"],
    )
    customSkuId: str = Field(
        ...,
        description="**Mandatory.** CustomSKU id (24-character ObjectId) for the product being viewed. Must belong to this client **and** carry the requested locale.",
        examples=["681aa2f1c4b21d0f8c9e0012"],
    )
    price: float = Field(
        ...,
        description="**Mandatory field**, but `0` is allowed and falls back to the SKU's `MSRP` for that locale. Send the actual basket price when you have it — it changes the premium.",
        examples=[449.99],
    )
    locale: str = Field(..., description="**Mandatory.** Locale of the storefront, e.g. `en_GB`.", examples=["en_GB"])
    currency: Optional[str] = Field(
        None,
        description="Currency override. Otherwise resolved from the SKU's locale data, then from `Locale_Params`.",
        examples=["GBP"],
    )
    purchaseDate: Optional[str] = Field(
        None,
        description="**`YYYY-MM-DD`.** Defaults to today, i.e. a new purchase. Any other format returns `400`.",
        examples=["2026-08-06"],
    )
    gtee: Optional[int] = Field(
        None,
        description="Guarantee duration override in months. Otherwise the SKU's labour guarantee, falling back to parts.",
        examples=[12],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "clientKey": "acme_uk_live",
                "customSkuId": "681aa2f1c4b21d0f8c9e0012",
                "price": 449.99,
                "locale": "en_GB",
                "currency": "GBP",
                "purchaseDate": None,
                "gtee": None,
            }
        }
    }


class WidgetQuoteRequest(WidgetPriceRequest):
    """Everything in `WidgetPriceRequest`, plus what the shopper actually chose."""

    productId: str = Field(..., description="**Mandatory.** The product id the shopper selected.", examples=["ACME-EW-STD"])
    optionId: Optional[str] = Field(
        None,
        description="The selected cover term, normally the `poc` value in months. Recorded on the quote as `selected.optionId`.",
        examples=["24"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "clientKey": "acme_uk_live",
                "customSkuId": "681aa2f1c4b21d0f8c9e0012",
                "price": 449.99,
                "locale": "en_GB",
                "currency": "GBP",
                "productId": "ACME-EW-STD",
                "optionId": "24",
            }
        }
    }


# ---------- Helpers ----------

def _to_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _to_float(val, default=0.0):
    try:
        return float(val)
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
    price = payload.price or _to_float(lsd.get("MSRP"))

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
    try:
        age_in_months = calculate_age_in_months(purchase_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid purchaseDate; expected YYYY-MM-DD")
    return assignment_request, age_in_months, client_doc, customsku_doc, lsd


def compute_options(payload: WidgetPriceRequest):
    """Run assignment + rating for a widget request. Returns (grouped, bracket, currency)."""
    assignment_request, age_in_months, _client_doc, _sku, _lsd = resolve_widget_inputs(payload)
    return _compute_options_from_assignment(assignment_request, age_in_months)


def _compute_options_from_assignment(assignment_request, age_in_months):
    """Run assignment + rating from already-resolved inputs. Returns (grouped, bracket, currency)."""
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

def _cache_read(custom_sku_id, locale, age, price, gtee, currency):
    doc = widget_cache_collection.find_one({
        "customSkuId": custom_sku_id,
        "locale": locale,
        "age": age,
        "gtee": gtee,
        "currency": currency,
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


def _cache_write(custom_sku_id, locale, age, price, gtee, bracket, currency, grouped):
    low, high = bracket if bracket else (price, price)
    widget_cache_collection.update_one(
        {
            "customSkuId": custom_sku_id,
            "locale": locale,
            "age": age,
            "gtee": gtee,
            "currency": currency,
            "priceLow": low,
            "priceHigh": high,
        },
        {"$set": {
            "customSkuId": custom_sku_id,
            "locale": locale,
            "age": age,
            "gtee": gtee,
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
    assignment_request, age_in_months, _client_doc, _sku, _lsd = resolve_widget_inputs(payload)
    price = assignment_request.price

    cached = _cache_read(
        payload.customSkuId, payload.locale, age_in_months, price,
        assignment_request.gtee, assignment_request.currency,
    )
    if cached:
        return cached.get("options", []), cached.get("currency")

    grouped, bracket, currency = _compute_options_from_assignment(assignment_request, age_in_months)
    if grouped:
        _cache_write(
            payload.customSkuId, payload.locale, age_in_months, price,
            assignment_request.gtee, bracket, currency, grouped,
        )
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

@router.post(
    "/widget_price",
    summary="Price cover options for the storefront widget (nothing stored)",
    response_description="The priced options to display, the currency, and the widget's checkout mode.",
    responses=secured({
        200: json_response("Options available for this product.", {
                "currency": "GBP",
                "mode": "redirect",
                "options": [
                    {
                        "product_id": "ACME-EW-STD",
                        "client": "ACME-UK",
                        "currency": "GBP",
                        "locale": "en_GB",
                        "category": "Dishwasher",
                        "age": 0,
                        "price": 449.99,
                        "multi_count": 1,
                        "source": "web",
                        "options": [
                            {"status": "ok", "poc": 24, "mode": "live", "rate": 71.34, "rounded_price": 71.49, "rounded_price_pence": 7149},
                            {"status": "ok", "poc": 36, "mode": "live", "rate": 96.18, "rounded_price": 96.49, "rounded_price_pence": 9649},
                        ],
                    }
                ],
            }),
        400: error("`customSkuId` is malformed, or `purchaseDate` is not `YYYY-MM-DD`.", "Invalid customSkuId"),
        403: error("The widget is disabled for this client, or the browser `Origin` is not on the client's allow-list.", "Origin not allowed"),
        404: error("Unknown `clientKey`, no such CustomSKU for this client and locale, or nothing is offerable.", "No protection options available for this product"),
    }),
)
def widget_price(payload: WidgetPriceRequest, request: Request, _: None = Depends(verify_token)):
    """
    Price the cover options to show in the storefront widget. **Read-only — no quote is stored.**

    Everything the pricing needs is resolved from the catalogue rather than a registered device:
    the CustomSKU supplies the category and, when `price`, `currency` or `gtee` are omitted, the
    MSRP, currency and guarantee for that locale. `purchaseDate` defaults to today, so the device
    is treated as new.

    Results come from a **cache** keyed on the SKU, locale, price band, age, guarantee and
    currency, which is why repeated views are fast and why an edit to the SKU refreshes it in the
    background. Use `POST /widget_quote/refresh` to force a rebuild.

    `mode` in the response is the client's configured checkout behaviour (`redirect` by default)
    — the widget uses it to decide how to proceed once the shopper picks an option.

    Beyond the bearer token, this endpoint is guarded by the client's own settings: a client with
    `widget_enabled: false` gets `403`, and a browser `Origin` outside the client's
    `allowed_domains` is rejected. Call `POST /widget_quote` once the shopper commits.
    """
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


@router.post(
    "/widget_quote",
    summary="Commit the shopper's widget selection as a stored quote",
    response_description="The id of the newly stored quote.",
    responses=secured({
        200: json_response("The quote was stored.", {"quote_id": "68a1c2d3e4b21d0f8c9e7712"}),
        400: error("`customSkuId` is malformed, or `purchaseDate` is not `YYYY-MM-DD`.", "Invalid customSkuId"),
        403: error("The widget is disabled for this client, or the browser `Origin` is not allowed.", "Origin not allowed"),
        404: error("Unknown `clientKey`, no such CustomSKU for this client and locale, or nothing is offerable.", "No protection options available for this product"),
    }),
)
def widget_quote(payload: WidgetQuoteRequest, request: Request, _: None = Depends(verify_token)):
    """
    Commit the shopper's choice: re-price the options and **persist them as a quote**.

    This is the write counterpart to `POST /widget_price` — same inputs plus `productId` and
    `optionId`, same pricing path. Options are recomputed here rather than trusted from the
    display call, so a tampered price cannot be carried into a quote.

    The stored quote records `customSkuId`, `locale`, `currency`, `source: "widget"` and the
    shopper's `selected` product and option. Only `quote_id` is returned; fetch the full quote
    with `GET /quote/{quote_id}`, or turn it into a payment with
    `POST /generate_payment_link`.

    Note the quote holds **every** priced option, not just the selected one — the selection is
    recorded alongside them rather than filtering them.
    """
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


@router.post(
    "/widget_quote/refresh",
    summary="Rebuild the widget price cache for one SKU, locale and price",
    response_description="Confirmation and how many option groups were cached.",
    responses=secured({
        200: json_response("The cache entry was rebuilt.", {"status": "ok", "cached_options": 1}),
        400: error("`customSkuId` is malformed, or `purchaseDate` is not `YYYY-MM-DD`.", "Invalid customSkuId"),
        404: error("Unknown `clientKey`, no such CustomSKU for this client and locale, or nothing to cache.", "No protection options to cache"),
    }),
)
def widget_quote_refresh(payload: WidgetPriceRequest, _: None = Depends(verify_token)):
    """
    **Operational endpoint.** Force a rebuild of the widget price cache for one CustomSKU,
    locale, price, age, guarantee and currency combination.

    Pricing is recomputed from the assignment rules and written straight to the cache, so the
    next `POST /widget_price` for the same combination serves the fresh figures. No quote is
    stored and nothing is returned to a shopper.

    You normally do not need this: the cache is warmed automatically whenever a CustomSKU is
    created or updated. Reach for it after changing rating configuration — which those hooks do
    not observe — or to verify a pricing change before it reaches a storefront.

    `cached_options` is the number of product groups written. Unlike `/widget_price`, this
    endpoint does **not** apply the widget's origin or enabled checks; it is not meant to be
    called from a browser.
    """
    assignment_request, age_in_months, _client_doc, _sku, _lsd = resolve_widget_inputs(payload)
    grouped, bracket, currency = _compute_options_from_assignment(assignment_request, age_in_months)
    if not grouped:
        raise HTTPException(status_code=404, detail="No protection options to cache")
    _cache_write(
        payload.customSkuId, payload.locale, age_in_months, assignment_request.price,
        assignment_request.gtee, bracket, currency, grouped,
    )
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
            # No MSRP yet — typically a SKU created before its MasterSKU price
            # landed. propagate_master_price re-warms it once enrichment fills
            # the price in; log so a cold cache is traceable to the cause.
            print(f"[WIDGET-CACHE] skipped {custom_sku_id} / {locale}: no MSRP")
            return
        payload = WidgetPriceRequest(
            clientKey=client_key, customSkuId=custom_sku_id, price=price, locale=locale
        )
        assignment_request, age_in_months, _client_doc, _sku, _lsd = resolve_widget_inputs(payload)
        grouped, bracket, currency = _compute_options_from_assignment(assignment_request, age_in_months)

        # Always drop existing cache rows for this SKU/locale: even when
        # recomputation yields no options (category/MSRP/guarantees changed
        # such that nothing matches), stale rows must not keep being served.
        _cache_invalidate(custom_sku_id, locale)
        if grouped:
            _cache_write(
                custom_sku_id, locale, age_in_months, assignment_request.price,
                assignment_request.gtee, bracket, currency, grouped,
            )
            print(f"[WIDGET-CACHE] warmed {custom_sku_id} / {locale}")
        else:
            print(
                f"[WIDGET-CACHE] no options for {custom_sku_id} / {locale} "
                f"(category={assignment_request.category!r} price={assignment_request.price} "
                f"gtee={assignment_request.gtee} currency={assignment_request.currency})"
            )
    except Exception as e:
        print(f"[WIDGET-CACHE] warm failed for {custom_sku_id}/{locale}: {e}")
