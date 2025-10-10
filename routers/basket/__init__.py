from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId
from datetime import datetime
import os
from utils.dependencies import verify_token

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
    product_name: Optional[str] = None
    product_description: Optional[str] = None
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

    # 3) Build the basket line-item from quote fields
    # Quote created by rate_request stores deviceId at root and grouped response fields in product/option
    device_id = quote.get("deviceId")

    # Core fields expected in example
    basket_item = {
        "deviceId": device_id,
        "quote_id": payload.quote_id,
        "product_id": product.get("product_id"),
        "client": product.get("client"),
        "currency": product.get("currency"),
        "locale": product.get("locale"),
        "category": product.get("category"),
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

    # 4) Create or append to Basket_Quotes by _id (basket_id)
    if payload.basket_id:
        # Append to existing basket
        try:
            bid = ObjectId(payload.basket_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

        update: Dict[str, Any] = {"$push": {"Basket": basket_item}}

        result = basket_collection.find_one_and_update(
            {"_id": bid},
            update,
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            raise HTTPException(status_code=404, detail="Basket not found for provided basket_id")
    else:
        # Create new basket document
        doc = {
            "Basket": [basket_item],
            "status": "draft",
            "created_at": datetime.utcnow(),
        }
        insert = basket_collection.insert_one(doc)
        result = basket_collection.find_one({"_id": insert.inserted_id})

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
def delete_basket_item(basket_id: str, device_id: str, _: None = Depends(verify_token)):
    """Delete all items in Basket array with matching deviceId and return updated doc."""
    try:
        bid = ObjectId(basket_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    # Pull all items with the deviceId
    update_result = basket_collection.update_one({"_id": bid}, {"$pull": {"Basket": {"deviceId": device_id}}})
    if update_result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Basket not found")

    # Fetch updated document to return
    doc = basket_collection.find_one({"_id": bid})
    if not doc:
        raise HTTPException(status_code=404, detail="Basket not found after update")
    return _serialize_basket_doc(doc)
