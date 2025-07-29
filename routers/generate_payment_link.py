from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Dict
from enum import Enum
import stripe
import os
import requests

router = APIRouter(tags=["Payment Links"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
TINYURL_API_KEY = os.getenv("TINYURL_API_KEY")
TINYURL_API_URL = "https://api.tinyurl.com/create"

class ModeEnum(str, Enum):
    payment = "payment"
    subscription = "subscription"

class CheckoutSessionRequest(BaseModel):
    product_name: str = Field(..., example="Test Product")
    unit_amount: int = Field(..., example=2500, description="Amount in the smallest currency unit (e.g. cents)")
    currency: str = Field(..., example="usd")
    quantity: int = Field(..., gt=0, example=1)
    # Subscription fields (ignored for payments)
    recurring_interval: Optional[str] = Field(default=None, example="month", description="For subscriptions: 'day', 'week', 'month', or 'year'")
    recurring_interval_count: Optional[int] = Field(default=1, example=1, description="For subscriptions: Number of intervals between billings")
    customer_email: Optional[EmailStr] = Field(default=None, example="customer@email.com")
    allow_promotion_codes: bool = Field(default=False)
    success_url: str = Field(..., example="https://yourdomain.com/success")
    cancel_url: str = Field(..., example="https://yourdomain.com/cancel")
    phone_number_collection: bool = Field(default=False)
    internal_reference: str = Field(..., example="order-12345")
    payment_method_types: Optional[List[str]] = Field(default=["card"], example=["card", "alipay"])
    mode: ModeEnum = Field(..., example="payment", description="Stripe session mode: payment or subscription")
    metadata: Optional[Dict[str, str]] = Field(default_factory=dict, example={"order_id": "1234"})
    locale: Optional[str] = Field(default=None, example="fr")

def build_line_items(request: CheckoutSessionRequest):
    price_data = {
        "currency": request.currency,
        "product_data": {"name": request.product_name},
        "unit_amount": request.unit_amount,
    }
    if request.mode == "subscription":
        price_data["recurring"] = {
            "interval": request.recurring_interval or "month",
            "interval_count": request.recurring_interval_count or 1
        }
    return [{
        "price_data": price_data,
        "quantity": request.quantity
    }]

def shorten_with_tinyurl(long_url: str) -> str:
    headers = {
        "Authorization": f"Bearer {TINYURL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"url": long_url}
    try:
        resp = requests.post(TINYURL_API_URL, json=payload, headers=headers, timeout=10)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"TinyURL error: {resp.text}")
        data = resp.json()
        short_url = data.get("data", {}).get("tiny_url")
        if not short_url:
            raise HTTPException(status_code=502, detail="No shortened URL returned from TinyURL")
        return short_url
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"TinyURL API error: {e}")

@router.post("/generate_checkout_session")
def generate_checkout_session(request: CheckoutSessionRequest):
    """
    Generate a Stripe Checkout Session and return the session URL, session id, and a TinyURL short link.
    """
    try:
        session_params = {
            "payment_method_types": request.payment_method_types or ["card"],
            "line_items": build_line_items(request),
            "mode": request.mode.value,  # Enum to string
            "allow_promotion_codes": request.allow_promotion_codes,
            "success_url": request.success_url + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": request.cancel_url,
            "phone_number_collection": {"enabled": request.phone_number_collection},
            "metadata": {**(request.metadata or {}), "internal_reference": request.internal_reference},
            "locale": request.locale if request.locale else None
        }
        if request.customer_email:
            session_params["customer_email"] = request.customer_email

        session = stripe.checkout.Session.create(**session_params)
        checkout_url = session.url
        short_url = shorten_with_tinyurl(checkout_url)
        return {
            "checkout_url": checkout_url,
            "checkout_url_short": short_url,
            "session_id": session.id,
            "expires_at": session.expires_at,
            "status": session.status
        }
    except stripe.error.StripeError as se:
        raise HTTPException(status_code=400, detail=f"Stripe error: {se.user_message or str(se)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating Stripe checkout session: {str(e)}")
