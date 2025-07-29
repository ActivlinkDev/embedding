from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
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
    arrayref: int
    email: Optional[str] = None  # Optional email field

@router.post("/generate_payment_link")
def generate_quote_payment_link(req: PaymentLinkRequest):
    try:
        clean_id = req.quote_id.strip()
        quote = quotes_collection.find_one({"_id": ObjectId(clean_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ObjectId format.")

    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    responses = quote.get("responses", [])
    if req.arrayref < 0 or req.arrayref >= len(responses):
        raise HTTPException(status_code=400, detail="Reference data does not exist, please check quote references")
    
    data = responses[req.arrayref]

    try:
        unit_amount = int(data["rounded_price_pence"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid or missing rounded_price_pence in quote")

    req_checkout = CheckoutSessionRequest(
        product_name=data.get("product_id", "Product"),
        unit_amount=unit_amount,
        currency=data.get("currency", "gbp").lower(),
        quantity=1,
        mode=ModeEnum(data.get("mode", "payment")),
        success_url="https://yourdomain.com/success",
        cancel_url="https://yourdomain.com/cancel",
        locale=data.get("lang", "en"),
        internal_reference=str(quote["_id"]),
        metadata={
            "client": data.get("client", ""),
            "source": data.get("source", ""),
        },
        customer_email=req.email if req.email else None
    )

    try:
        return generate_checkout_session(req_checkout)
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error during Stripe session creation.")
