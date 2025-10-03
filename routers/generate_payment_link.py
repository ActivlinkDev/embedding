import os
import stripe
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from typing import List, Optional
from datetime import datetime, timezone

# Environment variables
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
TINYURL_API_KEY = os.getenv("TINYURL_API_KEY")

if not STRIPE_SECRET_KEY:
    raise ValueError("Missing STRIPE_SECRET_KEY in environment variables")

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter()


# ----------------------------
# Request body model
# ----------------------------
class PaymentLinkRequest(BaseModel):
    # Existing fields
    quote_id: str
    product_id: str
    optionref: int
    email: EmailStr

    # ✅ New fields for product info
    product_name: str
    product_description: Optional[str] = None
    product_images: Optional[List[str]] = None  # must be HTTPS URLs
    locale: Optional[str] = None                # e.g. "en-GB"

    # Optional Stripe settings
    unit_amount: Optional[int] = None  # in minor units (pence/cents)
    currency: Optional[str] = "GBP"
    mode: Optional[str] = "payment"    # "payment" or "subscription"
    recurring_interval: Optional[str] = None
    recurring_interval_count: Optional[int] = None
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


# ----------------------------
# Response model
# ----------------------------
class PaymentLinkResponse(BaseModel):
    checkout_url: str
    checkout_url_short: Optional[str] = None
    session_id: str
    expires_at: datetime
    status: str


# ----------------------------
# Helper: shorten URL with TinyURL (optional)
# ----------------------------
def shorten_url(long_url: str) -> Optional[str]:
    if not TINYURL_API_KEY:
        return None
    try:
        response = requests.post(
            "https://api.tinyurl.com/create",
            headers={"Authorization": f"Bearer {TINYURL_API_KEY}",
                     "Content-Type": "application/json"},
            json={"url": long_url},
            timeout=10,
        )
        if response.status_code == 200:
            return response.json().get("data", {}).get("tiny_url")
    except Exception as e:
        print(f"⚠️ TinyURL error: {e}")
    return None


# ----------------------------
# Endpoint
# ----------------------------
@router.post("/generate_payment_link", response_model=PaymentLinkResponse)
async def generate_payment_link(request: PaymentLinkRequest):
    try:
        if not request.product_name:
            raise HTTPException(status_code=400, detail="product_name is required")

        if not request.unit_amount:
            raise HTTPException(status_code=400, detail="unit_amount is required")

        if not request.currency:
            raise HTTPException(status_code=400, detail="currency is required")

        if not request.success_url or not request.cancel_url:
            raise HTTPException(status_code=400, detail="success_url and cancel_url are required")

        # Build product_data for Stripe
        product_data = {
            "name": request.product_name,
        }
        if request.product_description:
            product_data["description"] = request.product_description
        if request.product_images:
            product_data["images"] = request.product_images

        # Handle recurring products if specified
        price_data = {
            "currency": request.currency,
            "unit_amount": request.unit_amount,
            "product_data": product_data,
        }
        if request.mode == "subscription" and request.recurring_interval:
            price_data["recurring"] = {
                "interval": request.recurring_interval,
                "interval_count": request.recurring_interval_count or 1,
            }

        # Create Stripe Checkout Session
        session = stripe.checkout.Session.create(
            mode=request.mode or "payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": price_data,
                "quantity": 1,
            }],
            customer_email=request.email,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            locale=request.locale,
            metadata={
                "quote_id": request.quote_id,
                "product_id": request.product_id,
                "optionref": str(request.optionref),
            },
        )

        # Shorten URL if possible
        short_url = shorten_url(session.url)

        return PaymentLinkResponse(
            checkout_url=session.url,
            checkout_url_short=short_url,
            session_id=session.id,
            expires_at=datetime.fromtimestamp(session.expires_at, tz=timezone.utc),
            status=session.status,
        )

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
