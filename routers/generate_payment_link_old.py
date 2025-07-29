from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, EmailStr, HttpUrl
from typing import List, Optional, Dict
import stripe
import os
import requests

router = APIRouter(tags=["Stripe Checkout"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
TINYURL_API_KEY = os.getenv("TINYURL_API_KEY")
TINYURL_API_URL = "https://api.tinyurl.com/create"

class CheckoutSessionRequest(BaseModel):
    price_id: str = Field(..., example="price_1NE5HZGsjgCM...")
    quantity: int = Field(..., gt=0, example=1)
    customer_email: Optional[EmailStr] = Field(default=None, example="customer@email.com")
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

def shorten_with_tinyurl(long_url: str) -> str:
    """
    Shorten a URL using TinyURL API. Returns the short URL or raises HTTPException.
    """
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
            "mode": request.mode,
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
        # Shorten the checkout URL using TinyURL
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
