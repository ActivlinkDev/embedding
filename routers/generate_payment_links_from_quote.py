from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from bson import ObjectId
from routers.generate_payment_link import generate_checkout_session, CheckoutSessionRequest, ModeEnum
from pymongo import MongoClient
import os

router = APIRouter(tags=["Payment Links"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
quotes_collection = db["Quotes"]

class PaymentLinkRequest(BaseModel):
    quote_id: str
    product_id: str
    optionref: int
    email: Optional[str] = None  # Optional email field
    product_name: Optional[str] = None
    product_description: Optional[str] = None
    product_images: Optional[List[str]] = None


@router.post("/generate_payment_link")
def generate_quote_payment_link(req: PaymentLinkRequest):
    # 1. Load quote from DB
    try:
        clean_id = req.quote_id.strip()
        quote = quotes_collection.find_one({"_id": ObjectId(clean_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ObjectId format.")

    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    # 2. Find the product in responses by product_id
    responses = quote.get("responses", [])
    product = next((r for r in responses if r.get("product_id") == req.product_id), None)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found in quote responses")

    # 3. Get the correct option
    options = product.get("options", [])
    if req.optionref < 0 or req.optionref >= len(options):
        raise HTTPException(status_code=400, detail="Optionref out of range for this product")

    option = options[req.optionref]

    # 4. Build CheckoutSessionRequest
    try:
        unit_amount = int(option["rounded_price_pence"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid or missing rounded_price_pence in option")

    # Use locale only if it exists
    locale = product.get("lang") if "lang" in product else None

    req_checkout = CheckoutSessionRequest(
        product_name=req.product_name or product.get("product_id", "Product"),
        product_description=req.product_description or product.get("product_description"),
        product_images=req.product_images or product.get("product_images"),
        unit_amount=unit_amount,
        currency=product.get("currency", "gbp").lower(),
        quantity=1,
        mode=ModeEnum(option.get("mode", "payment")),
        success_url="https://yourdomain.com/success",
        cancel_url="https://yourdomain.com/cancel",
        locale=locale,
        internal_reference=str(quote["_id"]),
        metadata={
            "client": product.get("client", ""),
            "source": product.get("source", ""),
            "quote_id": req.quote_id,
            "product_id": req.product_id,
            "optionref": str(req.optionref),
        },
        customer_email=req.email if req.email else None
    )

    try:
        return generate_checkout_session(req_checkout)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error during Stripe session creation: {e}")
