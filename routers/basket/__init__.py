from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId
from datetime import datetime
import os
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from .ratebasket import rate_basket, RateBasketRequest

router = APIRouter(tags=["Basket"])

# DB setup (reuse suite conventions)
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
quotes_collection = db["Quotes"]
basket_collection = db["Basket_Quotes"]
devices_collection = db["Devices"]


class AddToBasketRequest(BaseModel):
    """One line to add to a basket — either a purchase or a declined offer.

    **Which fields are required depends on `add_to_basket`:**

    | `add_to_basket` | Required |
    | --- | --- |
    | `true` (default) | `quote_id`, `product_id`, `optionref` |
    | `false` | `deviceId` (or a `quote_id` that carries one) |

    Everything else is optional. Omit `basket_id` to start a new basket; pass it to append to an
    existing one.
    """

    # When add_to_basket is true, quote_id is required. For skipped items (add_to_basket=false), quote_id is optional.
    quote_id: Optional[str] = Field(
        default=None,
        description="**Required when `add_to_basket` is true.** The `Quotes._id` this line is bought from. Optional for skipped items.",
        examples=["68a1c2d3e4b21d0f8c9e7712"],
    )
    # product_id/optionref are required only when add_to_basket is true
    product_id: Optional[str] = Field(
        default=None,
        description="**Required when `add_to_basket` is true.** Which product group inside the quote's `responses` is being bought.",
        examples=["ACME-EW-STD"],
    )
    optionref: Optional[int] = Field(
        default=None,
        ge=0,
        description="**Required when `add_to_basket` is true.** **Zero-based index** into that group's `options` array — the position, not the cover term.",
        examples=[0],
    )
    # Optional root-level metadata to persist when creating a new Basket_Quotes document
    client: Optional[str] = Field(
        default=None,
        description="Client identifier stored on the basket root. Only used when creating a new basket; derived from the quote when omitted.",
        examples=["ACME-UK"],
    )
    locale: Optional[str] = Field(
        default=None,
        description="Locale stored on the basket root. Only used when creating a new basket; derived from the quote when omitted.",
        examples=["en_GB"],
    )
    product_name: Optional[str] = Field(
        None,
        description="Display name for this line, carried through to checkout.",
        examples=["Acme Extended Warranty — 24 months"],
    )
    product_description: Optional[str] = Field(
        None,
        description="Display description for this line.",
        examples=["Parts and labour cover for your dishwasher"],
    )
    product_images: Optional[List[str]] = Field(
        default=None,
        description="Image URLs stored on the basket line and shown at checkout.",
        examples=[["https://cdn.example.com/img1.jpg"]],
    )
    make: Optional[str] = Field(
        default=None,
        description="Device make for this line. Overrides the value derived from the quote or the registered device.",
        examples=["Bosch"],
    )
    model: Optional[str] = Field(
        default=None,
        description="Device model for this line. Overrides the value derived from the quote or the registered device.",
        examples=["SMS6ZCI00G"],
    )
    promo_id: Optional[str] = Field(
        default=None,
        description="Promotion id to attach to this line or skipped entry.",
        examples=["10YP"],
    )
    add_to_basket: Optional[bool] = Field(
        default=True,
        description=(
            "`true` (default) adds a purchase line. `false` records the device in "
            "`skipped_items` instead — the customer was offered cover and declined."
        ),
        examples=[True],
    )
    # Optional category for skipped items when product_id is not provided
    category: Optional[str] = Field(
        default=None,
        description="Category for a skipped item when no `product_id` is given. Ignored for purchase lines.",
        examples=["Dishwasher"],
    )
    # Optional deviceId to support creating skipped entries without a quote
    deviceId: Optional[str] = Field(
        default=None,
        description="**Required for skipped items with no `quote_id`.** The device this entry refers to.",
        examples=["6820f1c9a4b21d0f8c9e4471"],
    )
    # Basket control: pass basket_id to append to an existing basket document
    basket_id: Optional[str] = Field(
        None,
        description="Existing basket to append to. Omit to create a new basket.",
        examples=["68b2d1f0a4b21d0f8c9e8801"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "quote_id": "68a1c2d3e4b21d0f8c9e7712",
                "product_id": "ACME-EW-STD",
                "optionref": 0,
                "client": "ACME-UK",
                "locale": "en_GB",
                "product_name": "Acme Extended Warranty — 24 months",
                "make": "Bosch",
                "model": "SMS6ZCI00G",
                "add_to_basket": True,
                "basket_id": None,
            }
        }
    }


def _serialize_basket_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Make Mongo document JSON-serializable (ObjectId -> str, datetime -> iso)."""
    out = dict(doc)
    _id = out.get("_id")
    if _id is not None:
        out["_id"] = str(_id)
    ca = out.get("created_at")
    if isinstance(ca, datetime):
        out["created_at"] = ca.isoformat()
    return out


@router.post(
    "/basket/add",
    summary="Add a line to a basket, or record a declined offer",
    response_description="The whole basket document after the change, including recalculated totals.",
    responses=secured({
        200: json_response("The line was added and the basket re-rated.", {
                "_id": "68b2d1f0a4b21d0f8c9e8801",
                "status": "draft",
                "client": "ACME-UK",
                "locale": "en_GB",
                "created_at": "2026-08-06T10:14:52.113000",
                "Basket": [
                    {
                        "line_id": "68b2d1f0a4b21d0f8c9e8802",
                        "deviceId": "6820f1c9a4b21d0f8c9e4471",
                        "quote_id": "68a1c2d3e4b21d0f8c9e7712",
                        "product_id": "ACME-EW-STD",
                        "product_name": "Acme Extended Warranty — 24 months",
                        "currency": "GBP",
                        "category": "Dishwasher",
                        "make": "Bosch",
                        "model": "SMS6ZCI00G",
                        "price": 449.99,
                        "poc": 24,
                        "mode": "live",
                        "rounded_price_pence": 7149,
                    }
                ],
                "skipped_items": [],
                "subtotal": 7149,
                "discount": 0,
                "final_total": 7149,
                "mode": "live",
            }),
        400: error(
            "A malformed id, a field missing for the chosen mode (`product_id`/`optionref`/"
            "`quote_id` when adding, `deviceId` when skipping), or `optionref` out of range.",
            "product_id is required when add_to_basket=true",
        ),
        404: error("The quote does not exist, or that `product_id` is not in it.", "Product not found in quote responses"),
        500: error("The basket could not be written.", "Failed to upsert basket"),
    }),
)
def add_to_basket(payload: AddToBasketRequest, _: None = Depends(verify_token)):
    """
    Add a line to a multi-device basket — or record that cover was **declined** for a device.

    **Two modes, set by `add_to_basket`:**

    - **`true` (default)** — append a purchase line. Requires `quote_id`, `product_id` and
      `optionref` (the zero-based index into that product's `options`). Price, currency,
      category and term are copied from the quote, so the line always reflects what was quoted.
    - **`false`** — append to `skipped_items` instead. Requires a `deviceId`, directly or via the
      quote. Nothing is charged; this exists so a journey can record which devices were offered
      cover and turned it down.

    **Basket lifecycle.** Omit `basket_id` to create a new basket (returned with its `_id`); pass
    it to append to an existing one. Every add re-rates the whole basket, so `subtotal`,
    `discount`, `final_total` and `best_rule` in the response are current — multi-device
    discounts appear as soon as the second line lands.

    Make and model are resolved in order: what you send, then the quote, then the registered
    device. Each line gets a `line_id`, which is the precise handle for
    `DELETE /basket/{basket_id}/item/{device_id}`.

    The full basket document is returned, not just the added line.
    """
    # 1) Load and validate the quote if provided
    quote = None
    if payload.quote_id:
        try:
            qid = ObjectId(payload.quote_id.strip())
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid quote_id; must be a valid ObjectId string")
        quote = quotes_collection.find_one({"_id": qid})
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")

    # 2) Branch based on action
    responses = (quote.get("responses", []) if quote else [])
    # deviceId: from quote if available, otherwise from payload
    device_id = (quote.get("deviceId") if quote else None) or (payload.deviceId or None)
    # Load registered device to enrich make/model when missing in quote/payload
    dev_doc = None
    if device_id:
        try:
            dev_doc = devices_collection.find_one({"_id": ObjectId(str(device_id))})
        except Exception:
            # If device_id isn't a valid ObjectId string, ignore silently
            dev_doc = None
    dev_identifiers = (dev_doc or {}).get("identifiers", {}) if isinstance((dev_doc or {}).get("identifiers", {}), dict) else {}
    dev_make = dev_identifiers.get("make") if isinstance(dev_identifiers.get("make"), str) else None
    dev_model = dev_identifiers.get("model") if isinstance(dev_identifiers.get("model"), str) else None
    # Respect optional make/model from payload when provided
    payload_make = (payload.make or "").strip() or None
    payload_model = (payload.model or "").strip() or None

    if payload.add_to_basket is False:
        # Build minimal skipped entry without requiring product_id/optionref
        locale_val = (payload.locale or (responses[0].get("locale") if responses else None) or (quote.get("locale") if quote else None))
        # Normalize category: treat empty string as None
        raw_cat = (payload.category or (responses[0].get("category") if responses else None))
        category_val = raw_cat if (raw_cat is not None and str(raw_cat).strip() != "") else None
        # Ensure we have a deviceId for the skipped entry
        if not device_id:
            raise HTTPException(status_code=400, detail="deviceId is required when quote_id is not provided for skipped items")
        skipped_item = {
            "quote_id": payload.quote_id,
            "deviceId": device_id,
            "locale": locale_val,
            # Only include category if present (non-empty)
            **({"category": category_val} if category_val is not None else {}),
            "make": payload_make
                     or ((quote.get("make") if isinstance(quote.get("make"), str) else None) if quote else None)
                     or ((quote.get("identifiers", {}) or {}).get("make") if quote else None)
                     or dev_make,
            "model": payload_model
                      or ((quote.get("model") if isinstance(quote.get("model"), str) else None) if quote else None)
                      or ((quote.get("identifiers", {}) or {}).get("model") if quote else None)
                      or dev_model,
        "created_at": datetime.utcnow(),
        # Per-line unique id to allow precise deletes
        "line_id": str(ObjectId()),
        }
    else:
        # Validate requirements for adding to basket
        if not payload.product_id:
            raise HTTPException(status_code=400, detail="product_id is required when add_to_basket=true")
        if payload.optionref is None:
            raise HTTPException(status_code=400, detail="optionref is required when add_to_basket=true")
        if not quote:
            raise HTTPException(status_code=400, detail="quote_id is required when add_to_basket=true")
        product = next((r for r in responses if r.get("product_id") == payload.product_id), None)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found in quote responses")
        options = product.get("options", [])
        if payload.optionref < 0 or payload.optionref >= len(options):
            raise HTTPException(status_code=400, detail="optionref out of range for this product")
        option = options[payload.optionref]

        basket_item = {
            "deviceId": device_id,
            "quote_id": payload.quote_id,
            "product_id": product.get("product_id"),
            # Optional display text for downstream UIs
            "product_name": payload.product_name,
            "product_description": payload.product_description,
            "product_images": payload.product_images,
            "currency": product.get("currency"),
            "category": product.get("category"),
            "make": payload_make
                     or (quote.get("make") if isinstance(quote.get("make"), str) else None)
                     or (quote.get("identifiers", {}) or {}).get("make")
                     or dev_make,
            "model": payload_model
                      or (quote.get("model") if isinstance(quote.get("model"), str) else None)
                      or (quote.get("identifiers", {}) or {}).get("model")
                      or dev_model,
            "age": product.get("age"),
            "price": product.get("price"),
            "multi_count": product.get("multi_count"),
            "source": product.get("source"),
            "lang": product.get("lang"),
            # Option-level
            "poc": option.get("poc"),
            "mode": option.get("mode"),
            "rate": option.get("rate"),
            "rounded_price": option.get("rounded_price"),
            "rounded_price_pence": option.get("rounded_price_pence"),
            # Per-line unique id to allow precise deletes
            "line_id": str(ObjectId()),
        }
        # Attach promo_id if provided
        if payload.promo_id:
            skipped_item["promo_id"] = payload.promo_id
        # Attach promo_id if provided
        if payload.promo_id:
            basket_item["promo_id"] = payload.promo_id

    # 3) Create or append to Basket_Quotes by _id (basket_id)
    if payload.basket_id:
        # Append to existing basket or skipped list
        try:
            bid = ObjectId(payload.basket_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

        update: Dict[str, Any]
        if payload.add_to_basket is False:
            update = {"$push": {"skipped_items": skipped_item}}
        else:
            update = {"$push": {"Basket": basket_item}}

        result = basket_collection.find_one_and_update(
            {"_id": bid},
            update,
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            raise HTTPException(status_code=404, detail="Basket not found for provided basket_id")
        # Re-rate only if item was added to Basket (not when skipping)
        if payload.add_to_basket is not False:
            try:
                rb = rate_basket(RateBasketRequest(basket_id=str(bid)))
                # Persist totals explicitly as a safeguard
                # Compute mode from current items
                doc_now = basket_collection.find_one({"_id": bid}) or {}
                items_now = (doc_now.get("Basket") or [])
                modes = {it.get("mode") for it in items_now if it.get("mode") is not None}
                mode_value = next(iter(modes)) if len(modes) == 1 else "mixed"
                basket_collection.update_one(
                    {"_id": bid},
                    {
                        "$set": {
                            "subtotal": int(getattr(rb, "subtotal", 0)),
                            "final_total": int(getattr(rb, "final_total", 0)),
                            "discount": max(0, int(getattr(rb, "subtotal", 0)) - int(getattr(rb, "final_total", 0))),
                            "best_rule": (getattr(rb, "best").dict() if getattr(rb, "best", None) else None),
                            "mode": mode_value,
                        }
                    },
                )
                # Refresh result with updated totals
                result = basket_collection.find_one({"_id": bid})
            except Exception:
                pass
    else:
        # Create new basket document depending on action
        # Choose root-level client/locale for the basket document
        if payload.add_to_basket is False:
            # For skipped only, prefer payload.client/locale else quote/responses[0]
            root_client = (payload.client or "").strip() or (quote.get("client") if quote else None)
            root_locale = (payload.locale or "").strip() or (responses[0].get("locale") if responses else None) or (quote.get("locale") if quote else None)
            # When building root doc, omit empty category values (already normalized on skipped_item)
            doc = {
                "Basket": [],
                "skipped_items": [skipped_item],
                "status": "draft",
                "created_at": datetime.utcnow(),
                "client": root_client,
                "locale": root_locale,
            }
        else:
            # Adding an item requires product context
            product_for_root = next((r for r in responses if r.get("product_id") == payload.product_id), None)
            root_client = (payload.client or "").strip() or (product_for_root or {}).get("client") or (quote.get("client") if quote else None)
            root_locale = (payload.locale or "").strip() or (product_for_root or {}).get("locale") or (quote.get("locale") if quote else None)
            doc = {
                "Basket": [basket_item],
                "status": "draft",
                "created_at": datetime.utcnow(),
                "client": root_client,
                "locale": root_locale,
            }
        insert = basket_collection.insert_one(doc)
        bid_new = insert.inserted_id
        # If we created a basket with an item, rate it now
        if payload.add_to_basket is not False:
            try:
                rb = rate_basket(RateBasketRequest(basket_id=str(bid_new)))
                # Persist totals explicitly as a safeguard
                doc_now = basket_collection.find_one({"_id": bid_new}) or {}
                items_now = (doc_now.get("Basket") or [])
                modes = {it.get("mode") for it in items_now if it.get("mode") is not None}
                mode_value = next(iter(modes)) if len(modes) == 1 else "mixed"
                basket_collection.update_one(
                    {"_id": bid_new},
                    {
                        "$set": {
                            "subtotal": int(getattr(rb, "subtotal", 0)),
                            "final_total": int(getattr(rb, "final_total", 0)),
                            "discount": max(0, int(getattr(rb, "subtotal", 0)) - int(getattr(rb, "final_total", 0))),
                            "best_rule": (getattr(rb, "best").dict() if getattr(rb, "best", None) else None),
                            "mode": mode_value,
                        }
                    },
                )
            except Exception:
                pass
        result = basket_collection.find_one({"_id": bid_new})

    if not result:
        # Extremely unlikely with upsert+return_document, but handle defensively
        raise HTTPException(status_code=500, detail="Failed to upsert basket")

    return _serialize_basket_doc(result)


@router.get(
    "/basket/{basket_id}",
    summary="Fetch a basket by id",
    response_description="The basket document as stored.",
    responses=secured({
        200: json_response("The basket was found.", {
                "_id": "68b2d1f0a4b21d0f8c9e8801",
                "status": "draft",
                "client": "ACME-UK",
                "locale": "en_GB",
                "created_at": "2026-08-06T10:14:52.113000",
                "Basket": [
                    {
                        "line_id": "68b2d1f0a4b21d0f8c9e8802",
                        "deviceId": "6820f1c9a4b21d0f8c9e4471",
                        "quote_id": "68a1c2d3e4b21d0f8c9e7712",
                        "product_id": "ACME-EW-STD",
                        "product_name": "Acme Extended Warranty — 24 months",
                        "currency": "GBP",
                        "category": "Dishwasher",
                        "make": "Bosch",
                        "model": "SMS6ZCI00G",
                        "price": 449.99,
                        "poc": 24,
                        "mode": "live",
                        "rounded_price_pence": 7149,
                    }
                ],
                "skipped_items": [],
                "subtotal": 7149,
                "discount": 0,
                "final_total": 7149,
                "mode": "live",
            }),
        400: error("`basket_id` is not a valid 24-character ObjectId.", "Invalid basket_id; must be a valid ObjectId string"),
        404: error("No basket with this id.", "Basket not found"),
    }),
)
def get_basket(basket_id: str, _: None = Depends(verify_token)):
    """
    Fetch a basket in full: its purchase lines (`Basket`), declined devices (`skipped_items`),
    and the totals from the last rating (`subtotal`, `discount`, `final_total`, `best_rule`).

    Path parameter `basket_id` is mandatory. Totals are returned **as last calculated**, not
    recomputed on read — they are refreshed when lines are added or when `POST /basket/rate` is
    called.

    `status` is `draft` until payment; `POST /basket/payment/create` moves it on.
    """
    try:
        bid = ObjectId(basket_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    doc = basket_collection.find_one({"_id": bid})
    if not doc:
        raise HTTPException(status_code=404, detail="Basket not found")
    return _serialize_basket_doc(doc)


@router.delete(
    "/basket/{basket_id}/item/{device_id}",
    summary="Remove a purchase line from a basket",
    response_description="The basket document after the removal.",
    responses=secured({
        400: error("`basket_id` is not a valid 24-character ObjectId.", "Invalid basket_id; must be a valid ObjectId string"),
        404: error("No basket with this id.", "Basket not found"),
    }),
)
def delete_basket_item(
    basket_id: str,
    device_id: str,
    poc: Optional[int] = Query(None, description="Filter by term (months) to target a single item"),
    product_id: Optional[str] = Query(None, description="Filter by product_id to target a single item"),
    rounded_price_pence: Optional[int] = Query(None, description="Filter by rounded_price_pence to target a single item"),
    mode: Optional[str] = Query(None, description="Filter by mode to target a single item"),
    quote_id: Optional[str] = Query(None, description="Filter by originating quote id to target a single item"),
    line_id: Optional[str] = Query(None, description="Optional per-line id to delete a single line precisely"),
    _: None = Depends(verify_token),
):
    """
    Remove a purchase line from a basket.

    `basket_id` and `device_id` are mandatory; every query parameter is an optional narrowing
    filter.

    **Removal matches every line that fits the criteria.** With `device_id` alone, all lines for
    that device go — which is what you want when a device is removed from the order, but not when
    it has two cover terms in the basket. To delete exactly one line, pass the **`line_id`** from
    the basket document: it is matched on its own and ignores every other filter. Failing that,
    narrow with `poc`, `product_id`, `rounded_price_pence`, `mode` or `quote_id`.

    Removing a line that does not exist is **not** an error — the basket is returned unchanged.
    Only a missing basket returns `404`. Note that totals are **not** re-rated here; call
    `POST /basket/rate` afterwards to refresh them.
    """
    try:
        bid = ObjectId(basket_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    # If a per-line id is provided, target that specifically. Otherwise build precise pull criteria
    if line_id:
        pull_criteria: Dict[str, Any] = {"line_id": line_id}
    else:
        pull_criteria: Dict[str, Any] = {"deviceId": device_id}
        if poc is not None:
            pull_criteria["poc"] = int(poc)
        if product_id:
            pull_criteria["product_id"] = product_id
        if rounded_price_pence is not None:
            pull_criteria["rounded_price_pence"] = int(rounded_price_pence)
        if mode:
            pull_criteria["mode"] = mode
        if quote_id:
            pull_criteria["quote_id"] = quote_id

    # Pull items matching the criteria (ideally 1)
    update_result = basket_collection.update_one({"_id": bid}, {"$pull": {"Basket": pull_criteria}})
    if update_result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Basket not found")

    # Fetch updated document to return
    doc = basket_collection.find_one({"_id": bid})
    if not doc:
        raise HTTPException(status_code=404, detail="Basket not found after update")
    return _serialize_basket_doc(doc)


@router.delete(
    "/basket/{basket_id}/skipped/{device_id}",
    summary="Remove a declined-offer entry from a basket",
    response_description="The basket document after the removal.",
    responses=secured({
        400: error("`basket_id` is not a valid 24-character ObjectId.", "Invalid basket_id; must be a valid ObjectId string"),
        404: error("No basket with this id.", "Basket not found"),
    }),
)
def delete_skipped_item(
    basket_id: str,
    device_id: str,
    quote_id: Optional[str] = Query(None, description="Filter by originating quote id to target a single skipped entry"),
    category: Optional[str] = Query(None, description="Filter by category to target a single skipped entry"),
    make: Optional[str] = Query(None, description="Filter by make to target a single skipped entry"),
    model: Optional[str] = Query(None, description="Filter by model to target a single skipped entry"),
    line_id: Optional[str] = Query(None, description="Optional per-line id to delete a single skipped line precisely"),
    _: None = Depends(verify_token),
):
    """
    Remove an entry from a basket's `skipped_items` — for instance when a customer changes their
    mind and now wants cover for a device they previously declined.

    `basket_id` and `device_id` are mandatory; the query parameters narrow the match.

    **Removal matches every entry that fits the criteria**, so `device_id` alone clears all
    skipped entries for that device. Pass the **`line_id`** from the basket document to remove
    exactly one; it takes precedence over every other filter.

    Removing something that is not there is not an error — the basket comes back unchanged. Only
    a missing basket returns `404`. Purchase lines are untouched; use
    `DELETE /basket/{basket_id}/item/{device_id}` for those.
    """
    try:
        bid = ObjectId(basket_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    # If a per-line id is provided, target that specifically. Otherwise build precise pull criteria
    if line_id:
        pull_criteria: Dict[str, Any] = {"line_id": line_id}
    else:
        pull_criteria: Dict[str, Any] = {"deviceId": device_id}
        if quote_id:
            pull_criteria["quote_id"] = quote_id
        if category:
            pull_criteria["category"] = category
        if make:
            pull_criteria["make"] = make
        if model:
            pull_criteria["model"] = model

    update_result = basket_collection.update_one({"_id": bid}, {"$pull": {"skipped_items": pull_criteria}})
    if update_result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Basket not found")

    doc = basket_collection.find_one({"_id": bid})
    if not doc:
        raise HTTPException(status_code=404, detail="Basket not found after update")
    return _serialize_basket_doc(doc)
