from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, EmailStr, HttpUrl
from typing import List, Optional, Dict
from enum import Enum
import stripe
import os
import logging
import requests

from utils.api_docs import error, json_response

logger = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["Payments"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
TINYURL_API_KEY = os.getenv("TINYURL_API_KEY")
TINYURL_API_URL = "https://api.tinyurl.com/create"

class ModeEnum(str, Enum):
    """`payment` charges once; `subscription` bills on a recurring schedule."""

    payment = "payment"
    subscription = "subscription"

class CheckoutSessionRequest(BaseModel):
    """Everything needed to build a Stripe Checkout Session.

    **Mandatory:** `product_name`, `unit_amount`, `currency`, `quantity`, `success_url`,
    `cancel_url`, `internal_reference` and `mode`. Everything else has a working default.
    """

    # Core product info
    product_name: str = Field(
        ...,
        description="**Mandatory.** Product name shown on the Stripe Checkout page.",
        examples=["Acme Extended Warranty — 24 months"],
    )
    product_description: Optional[str] = Field(
        default=None,
        description="Sub-heading under the product name on the Checkout page.",
        examples=["Extended 3-year protection for your device"],
    )
    product_images: Optional[List[HttpUrl]] = Field(
        default=None,
        description="Absolute image URLs shown on the Checkout page. Must be publicly reachable by Stripe.",
        examples=[["https://cdn.example.com/images/cover.png"]],
    )

    unit_amount: int = Field(
        ...,
        description=(
            "**Mandatory.** Amount in the **smallest currency unit** — pence, cents. "
            "£71.49 is `7149`, not `71.49`. `rounded_price_pence` from `/rate_request` is "
            "already in this form."
        ),
        examples=[7149],
    )
    currency: str = Field(..., description="**Mandatory.** ISO 4217 code, lower-case for Stripe.", examples=["gbp"])
    quantity: int = Field(..., gt=0, description="**Mandatory.** Number of units. Must be greater than 0.", examples=[1])

    # Subscription fields (ignored for payments)
    recurring_interval: Optional[str] = Field(
        default=None,
        description="**Subscriptions only** — `day`, `week`, `month` or `year`. Defaults to `month`. Ignored when `mode` is `payment`.",
        examples=["month"],
    )
    recurring_interval_count: Optional[int] = Field(
        default=1,
        description="**Subscriptions only** — intervals between billings. Ignored when `mode` is `payment`.",
        examples=[1],
    )

    # Customer / session details
    customer_email: Optional[EmailStr] = Field(
        default=None,
        description="Pre-fills the email field. Must be a valid address if supplied.",
        examples=["jane.okafor@example.com"],
    )
    customer_phone: Optional[str] = Field(
        default=None,
        description=(
            "E.164 phone number. Stripe Checkout can only pre-fill the phone field from an "
            "existing Customer, so supplying this creates or reuses a Stripe Customer carrying "
            "the number and attaches it to the session. If that lookup fails, checkout still "
            "proceeds with email pre-fill only."
        ),
        examples=["+447700900123"],
    )
    allow_promotion_codes: bool = Field(
        default=False,
        description="Show the promotion-code box on the Checkout page.",
        examples=[False],
    )
    success_url: str = Field(
        ...,
        description="**Mandatory.** Where Stripe sends the customer after payment. `?session_id={CHECKOUT_SESSION_ID}` is appended automatically.",
        examples=["https://shop.example.com/cover/success"],
    )
    cancel_url: str = Field(
        ...,
        description="**Mandatory.** Where Stripe sends the customer if they abandon checkout.",
        examples=["https://shop.example.com/cover/cancel"],
    )
    phone_number_collection: bool = Field(
        default=True,
        description="Ask for a phone number during checkout. On by default.",
        examples=[True],
    )
    internal_reference: str = Field(
        ...,
        description=(
            "**Mandatory.** Your own reference for this payment. Stored in the session metadata "
            "and echoed back by the Stripe webhook — this is how a payment is reconciled to a quote."
        ),
        examples=["quote-68a1c2d3e4b21d0f8c9e7712"],
    )
    payment_method_types: Optional[List[str]] = Field(
        default=["card"],
        description="Stripe payment method types to offer. Defaults to card only.",
        examples=[["card"]],
    )
    mode: ModeEnum = Field(
        ...,
        description="**Mandatory.** `payment` for a one-off charge, `subscription` for recurring billing.",
        examples=["payment"],
    )
    metadata: Optional[Dict[str, str]] = Field(
        default_factory=dict,
        description="Extra key/value pairs stored on the Stripe session. String values only.",
        examples=[{"quote_id": "68a1c2d3e4b21d0f8c9e7712", "device_id": "6820f1c9a4b21d0f8c9e4471"}],
    )
    locale: Optional[str] = Field(
        default=None,
        description="Stripe Checkout display language, e.g. `en`, `fr`. Stripe auto-detects when omitted.",
        examples=["en"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "product_name": "Acme Extended Warranty — 24 months",
                "product_description": "Parts and labour cover for your dishwasher",
                "unit_amount": 7149,
                "currency": "gbp",
                "quantity": 1,
                "customer_email": "jane.okafor@example.com",
                "customer_phone": "+447700900123",
                "success_url": "https://shop.example.com/cover/success",
                "cancel_url": "https://shop.example.com/cover/cancel",
                "internal_reference": "quote-68a1c2d3e4b21d0f8c9e7712",
                "mode": "payment",
                "metadata": {"quote_id": "68a1c2d3e4b21d0f8c9e7712"},
                "locale": "en",
            }
        }
    }

def build_line_items(request: CheckoutSessionRequest):
    price_data = {
        "currency": request.currency,
        "product_data": {
            "name": request.product_name,
        },
        "unit_amount": request.unit_amount,
    }

    # ✅ Add description if provided
    if request.product_description:
        price_data["product_data"]["description"] = request.product_description

    # ✅ Add images if provided
    if request.product_images:
        price_data["product_data"]["images"] = request.product_images

    if request.mode == "subscription":
        price_data["recurring"] = {
            "interval": request.recurring_interval or "month",
            "interval_count": request.recurring_interval_count or 1
        }

    return [{
        "price_data": price_data,
        "quantity": request.quantity
    }]

def _resolve_stripe_customer_id(email: Optional[str], phone: str) -> str:
    """Find or create a Stripe Customer carrying the phone so Checkout pre-fills it."""
    customer = None
    if email:
        existing = stripe.Customer.list(email=email, limit=1)
        if existing.data:
            customer = existing.data[0]
    if customer is None:
        try:
            found = stripe.Customer.search(query=f"phone:'{phone}'", limit=1)
            if found.data:
                customer = found.data[0]
        except stripe.error.StripeError:
            customer = None
    if customer is not None:
        updates = {}
        if customer.get("phone") != phone:
            updates["phone"] = phone
        if email and customer.get("email") != email:
            updates["email"] = email
        if updates:
            customer = stripe.Customer.modify(customer["id"], **updates)
        logger.info(
            "checkout prefill: reusing customer %s (phone=%s, email=%s)",
            customer["id"], customer.get("phone"), customer.get("email"),
        )
        return customer["id"]
    create_params = {"phone": phone}
    if email:
        create_params["email"] = email
    created = stripe.Customer.create(**create_params)
    logger.info(
        "checkout prefill: created customer %s (phone=%s, email=%s)",
        created["id"], created.get("phone"), created.get("email"),
    )
    return created["id"]

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

@router.post(
    "/generate_checkout_session",
    summary="Create a Stripe Checkout session and a short payment link",
    response_description="The checkout URL, a shortened link, the session id, and pre-fill diagnostics.",
    responses={
        200: json_response(
            "The session was created and the link shortened.",
            {
                "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_a1B2c3D4e5F6",
                "checkout_url_short": "https://tinyurl.com/2p8kd4xr",
                "session_id": "cs_test_a1B2c3D4e5F6",
                "expires_at": 1786000000,
                "status": "open",
                "customer": "cus_QxYz123456",
                "prefill_phone": "+447700900123",
                "prefill_email": "jane.okafor@example.com",
            },
        ),
        400: error("Stripe rejected the session — bad currency, amount, or URL.", "Stripe error: Invalid currency: xyz"),
        500: error("The session could not be created, or the link shortener failed.", "TinyURL API error: ..."),
        502: error("TinyURL accepted the request but returned no shortened URL.", "No shortened URL returned from TinyURL"),
    },
)
def generate_checkout_session(request: CheckoutSessionRequest):
    """
    Create a Stripe Checkout session for a cover purchase and return a link the customer can pay
    through.

    The line item is built from `product_name`, `unit_amount`, `currency` and `quantity`.
    **`unit_amount` is in the smallest currency unit** — pass `rounded_price_pence` from
    `/rate_request` directly, never the decimal price.

    `internal_reference` is stored in the session metadata and comes back on the Stripe webhook,
    which is how a completed payment is matched to the quote that produced it. Set it to
    something you can reconcile.

    **Pre-fill behaviour.** `customer_email` pre-fills the email field on its own. A
    `customer_phone` additionally causes a Stripe Customer to be found or created carrying that
    number, since Checkout can only pre-fill a phone from an existing Customer. If that lookup
    fails, checkout is **not** blocked — it falls back to email pre-fill, and the `customer`,
    `prefill_phone` and `prefill_email` fields in the response tell you what actually reached
    Stripe.

    **This endpoint is not authenticated** and does not consult a quote — it charges whatever
    amount it is given, so only call it from a trusted server-side context, never straight from a
    browser. To create a link from a stored quote instead, use `POST /generate_payment_link`.

    Both a full `checkout_url` and a shortened `checkout_url_short` (via TinyURL) are returned;
    the short form is the one to put in an SMS.
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
        customer_phone = (request.customer_phone or "").strip()
        if customer_phone:
            try:
                session_params["customer"] = _resolve_stripe_customer_id(
                    request.customer_email, customer_phone
                )
            except stripe.error.StripeError as ce:
                # Don't block checkout if customer lookup/creation fails;
                # fall back to plain email pre-fill below.
                logger.warning(
                    "checkout prefill: customer resolution failed for phone=%s email=%s: %s",
                    customer_phone, request.customer_email, ce,
                )
        else:
            logger.info(
                "checkout prefill: no phone received (email=%s, ref=%s)",
                request.customer_email, request.internal_reference,
            )
        # Stripe rejects sessions with both `customer` and `customer_email`
        if "customer" not in session_params and request.customer_email:
            session_params["customer_email"] = request.customer_email

        session = stripe.checkout.Session.create(**session_params)
        checkout_url = session.url
        short_url = shorten_with_tinyurl(checkout_url)
        return {
            "checkout_url": checkout_url,
            "checkout_url_short": short_url,
            "session_id": session.id,
            "expires_at": session.expires_at,
            "status": session.status,
            # Diagnostics: confirm what reached Stripe so prefill issues are visible
            "customer": getattr(session, "customer", None),
            "prefill_phone": customer_phone or None,
            "prefill_email": str(request.customer_email) if request.customer_email else None,
        }
    except stripe.error.StripeError as se:
        raise HTTPException(status_code=400, detail=f"Stripe error: {se.user_message or str(se)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating Stripe checkout session: {str(e)}")
