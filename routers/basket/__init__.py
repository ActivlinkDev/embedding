from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId
from datetime import datetime
import os
from utils.dependencies import verify_token
from .ratebasket import rate_basket, RateBasketRequest

router = APIRouter(tags=["Basket"])

# DB setup (reuse suite conventions)
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
quotes_collection = db["Quotes"]
basket_collection = db["Basket_Quotes"]


class AddToBasketRequest(BaseModel):
    quote_id: str = Field(..., description="Quotes._id as string")
    product_id: str = Field(..., description="Group product_id inside quote.responses")
    optionref: int = Field(..., ge=0, description="Index into the group's options array")
    # Optional root-level metadata to persist when creating a new Basket_Quotes document
    client: Optional[str] = Field(
        default=None,
        description="Client identifier to store on the Basket_Quotes root when creating a new basket",
        example="activlink",
    )
    locale: Optional[str] = Field(
        default=None,
        description="Locale to store on the Basket_Quotes root when creating a new basket",
        example="en-GB",
    )
    product_name: Optional[str] = None
    product_description: Optional[str] = None
    product_images: Optional[List[str]] = Field(
        default=None,
        description="Array of product image URLs to persist on the basket line-item",
        example=["https://cdn.example.com/img1.jpg", "https://cdn.example.com/img2.jpg"],
    )
    make: Optional[str] = Field(
        default=None,
        description="Optional device make to store on the basket line item (overrides value derived from quote if provided)",
        example="Apple",
    )
    model: Optional[str] = Field(
        default=None,
        description="Optional device model to store on the basket line item (overrides value derived from quote if provided)",
        example="iPhone 13",
    )
    add_to_basket: Optional[bool] = Field(
        default=True,
        description="If true, append as a basket line item. If false, store quote/device in 'skipped_items' instead.",
    )
    # Basket control: pass basket_id to append to an existing basket document
    basket_id: Optional[str] = Field(None, description="Existing Basket_Quotes _id to append to")


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


@router.post("/basket/add")
def add_to_basket(payload: AddToBasketRequest, _: None = Depends(verify_token)):
    # 1) Load and validate the quote
    try:
        qid = ObjectId(payload.quote_id.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid quote_id; must be a valid ObjectId string")

    quote = quotes_collection.find_one({"_id": qid})
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    # 2) Find the grouped product and option within the quote
    responses = quote.get("responses", [])
    product = next((r for r in responses if r.get("product_id") == payload.product_id), None)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found in quote responses")

    options = product.get("options", [])
    if payload.optionref < 0 or payload.optionref >= len(options):
        raise HTTPException(status_code=400, detail="optionref out of range for this product")

    option = options[payload.optionref]

    # 3) Build the basket line-item (or skipped entry) from quote fields
    # Quote created by rate_request stores deviceId at root and grouped response fields in product/option
    device_id = quote.get("deviceId")

    # Core fields for normal basket items
    # Respect optional make/model from payload when provided
    payload_make = (payload.make or "").strip() or None
    payload_model = (payload.model or "").strip() or None

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
                 or (quote.get("identifiers", {}) or {}).get("make"),
        "model": payload_model
                  or (quote.get("model") if isinstance(quote.get("model"), str) else None)
                  or (quote.get("identifiers", {}) or {}).get("model"),
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
    }

    # Minimal record for skipped items
    skipped_item = {
        "quote_id": payload.quote_id,
        "deviceId": device_id,
        "locale": product.get("locale") or quote.get("locale"),
        "category": product.get("category"),
        "make": payload_make
                 or (quote.get("make") if isinstance(quote.get("make"), str) else None)
                 or (quote.get("identifiers", {}) or {}).get("make"),
        "model": payload_model
                  or (quote.get("model") if isinstance(quote.get("model"), str) else None)
                  or (quote.get("identifiers", {}) or {}).get("model"),
        "created_at": datetime.utcnow(),
    }

    # 4) Create or append to Basket_Quotes by _id (basket_id)
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
        # Choose root-level client/locale for the basket document (payload preferred, fallback to product/quote)
        root_client = (payload.client or "").strip() or product.get("client") or quote.get("client")
        root_locale = (payload.locale or "").strip() or product.get("locale") or quote.get("locale")
        if payload.add_to_basket is False:
            doc = {
                "Basket": [],
                "skipped_items": [skipped_item],
                "status": "draft",
                "created_at": datetime.utcnow(),
                "client": root_client,
                "locale": root_locale,
            }
        else:
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


@router.get("/basket/{basket_id}")
def get_basket(basket_id: str, _: None = Depends(verify_token)):
    """Return the full basket document by _id."""
    try:
        bid = ObjectId(basket_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    doc = basket_collection.find_one({"_id": bid})
    if not doc:
        raise HTTPException(status_code=404, detail="Basket not found")
    return _serialize_basket_doc(doc)


@router.delete("/basket/{basket_id}/item/{device_id}")
def delete_basket_item(
    basket_id: str,
    device_id: str,
    poc: Optional[int] = Query(None, description="Filter by term (months) to target a single item"),
    product_id: Optional[str] = Query(None, description="Filter by product_id to target a single item"),
    rounded_price_pence: Optional[int] = Query(None, description="Filter by rounded_price_pence to target a single item"),
    mode: Optional[str] = Query(None, description="Filter by mode to target a single item"),
    quote_id: Optional[str] = Query(None, description="Filter by originating quote id to target a single item"),
    _: None = Depends(verify_token),
):
    """Delete a single item in Basket array by deviceId and optional narrowing filters, then return updated doc.

    Note: MongoDB $pull removes all matches. With provided filters (e.g. deviceId + poc), we expect to uniquely match 1 item.
    For absolute precision, consider migrating to per-line unique IDs in future.
    """
    try:
        bid = ObjectId(basket_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    # Build precise pull criteria
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
