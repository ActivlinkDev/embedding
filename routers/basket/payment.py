from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, EmailStr
from typing import Optional, Dict, Any, List
from bson import ObjectId
from pymongo import MongoClient
import os

from utils.dependencies import verify_token
from routers.generate_payment_link import (
    generate_checkout_session,
    CheckoutSessionRequest,
    ModeEnum,
)

router = APIRouter(tags=["Basket"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
basket_collection = db["Basket_Quotes"]


class BasketPaymentRequest(BaseModel):
    basket_id: str = Field(..., description="Basket_Quotes _id as string")
    email: Optional[EmailStr] = Field(None, description="Customer email for Stripe session")
    product_name: Optional[str] = Field(None, description="Override product name for Stripe")
    product_description: Optional[str] = Field(None, description="Override product description for Stripe")
    product_images: Optional[List[str]] = Field(None, description="Override product images (URLs) for Stripe")


def _extract_currency(items: list[dict[str, Any]]) -> str:
    for it in items:
        cur = (it or {}).get("currency")
        if cur:
            return str(cur).lower()
    return "gbp"  # default fallback


def _extract_locale(items: list[dict[str, Any]]) -> Optional[str]:
    for it in items:
        loc = (it or {}).get("lang") or (it or {}).get("locale")
        if loc:
            return str(loc)
    return None


def _extract_client(items: list[dict[str, Any]]) -> str:
    for it in items:
        c = (it or {}).get("client")
        if c:
            return str(c)
    return ""


def _extract_source(items: list[dict[str, Any]]) -> str:
    for it in items:
        s = (it or {}).get("source")
        if s:
            return str(s)
    return ""


@router.post("/basket/payment/create")
def create_basket_payment_session(req: BasketPaymentRequest, _: None = Depends(verify_token)):
    # 1) Load basket
    try:
        bid = ObjectId(req.basket_id.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    basket = basket_collection.find_one({"_id": bid})
    if not basket:
        raise HTTPException(status_code=404, detail="Basket not found")

    items = basket.get("Basket", []) or []
    if not items:
        raise HTTPException(status_code=400, detail="Basket is empty")

    # 2) Amount: prefer final_total if set, otherwise subtotal, otherwise sum items
    final_total = basket.get("final_total")
    subtotal = basket.get("subtotal")
    if isinstance(final_total, int) and final_total > 0:
        amount_minor = final_total
    elif isinstance(subtotal, int) and subtotal > 0:
        amount_minor = subtotal
    else:
        # Fallback: compute from items' rounded_price_pence
        total = 0
        for it in items:
            rp = (it or {}).get("rounded_price_pence")
            if isinstance(rp, (int, float)):
                total += int(rp)
            else:
                r = (it or {}).get("rounded_price")
                if isinstance(r, (int, float)):
                    total += int(round(float(r) * 100))
        if total <= 0:
            raise HTTPException(status_code=400, detail="Cannot determine basket total")
        amount_minor = total

    # 3) Currency/locale/mode
    currency = _extract_currency(items)
    locale = _extract_locale(items)
    mode_value = basket.get("mode") or (items[0].get("mode") if items else "payment")
    try:
        mode_enum = ModeEnum(mode_value)
    except Exception:
        mode_enum = ModeEnum.payment

    # 4) Build request for Stripe
    best_rule = basket.get("best_rule") or {}
    product_name = req.product_name or best_rule.get("name") or basket.get("name") or "Basket checkout"
    product_description = req.product_description or basket.get("description") or "Basket items checkout"
    product_images = req.product_images

    req_checkout = CheckoutSessionRequest(
        product_name=product_name,
        product_description=product_description,
        product_images=product_images,
        unit_amount=int(amount_minor),
        currency=currency,
        quantity=1,
        mode=mode_enum,
        success_url="https://yourdomain.com/success",
        cancel_url="https://frontend-production-7798.up.railway.app/lookup",
        locale=locale,
        internal_reference=str(basket["_id"]),
        metadata={
            "basket_id": str(basket["_id"]),
            "client": _extract_client(items),
            "source": _extract_source(items),
        },
        customer_email=req.email if req.email else None,
    )

    # 5) Create session via shared helper
    try:
        return generate_checkout_session(req_checkout)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error during Stripe session creation: {e}")
