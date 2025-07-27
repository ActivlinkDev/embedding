from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Dict
import stripe
import os

router = APIRouter(tags=["Stripe Checkout"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

class CheckoutSessionRequest(BaseModel):
    price_id: str = Field(..., example="price_1NE5HZGsjgCM...")
    quantity: int = Field(..., gt=0, example=1)
    customer_email: EmailStr = Field(..., example="customer@email.com")
    allow_promotion_codes: bool = Field(default=False)
    success_url: str = Field(..., example="https://yourdomain.com/success")
    cancel_url: str = Field(..., example="https://yourdomain.com/cancel")
    phone_number_collection: bool = Field(default=False)
    internal_reference: str = Field(..., example="order-12345")
    payment_method_types: Optional[List[str]] = Field(default=["card"], example=["card", "alipay"])
    mode: str = Field(..., example="payment", description="Stripe session mode: payment, setup, or subscription")
    metadata: Optional[Dict[str, str]] = Field(default_factory=dict, example={"order_id": "1234"})
    locale: Optional[str] = Field(default=None, example="fr")

def build_line_items(request: CheckoutSessionRequest):
    return [{
        "price": request.price_id,
        "quantity": request.quantity
    }]

@router.post("/generate_checkout_session")
def generate_checkout_session(request: CheckoutSessionRequest):
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=request.payment_method_types or ["card"],
            line_items=build_line_items(request),
            mode=request.mode,
            customer_email=request.customer_email,
            allow_promotion_codes=request.allow_promotion_codes,
            success_url=request.success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.cancel_url,
            phone_number_collection={"enabled": request.phone_number_collection},
            metadata={**(request.metadata or {}), "internal_reference": request.internal_reference},
            locale=request.locale if request.locale else None
        )
        return {
            "checkout_url": session.url,
            "session_id": session.id
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error creating Stripe checkout session: {str(e)}")
