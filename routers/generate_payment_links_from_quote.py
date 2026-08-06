from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from bson import ObjectId
from routers.generate_payment_link import generate_checkout_session, CheckoutSessionRequest, ModeEnum
from pymongo import MongoClient
import os

from utils.api_docs import error, json_response

router = APIRouter(tags=["Payments"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
quotes_collection = db["Quotes"]

class PaymentLinkRequest(BaseModel):
    """Which option of a stored quote the customer is buying."""

    quote_id: str = Field(
        ...,
        description="**Mandatory.** The quote to charge against, as returned by `/rate_request` or `/widget_quote`.",
        examples=["68a1c2d3e4b21d0f8c9e7712"],
    )
    product_id: str = Field(
        ...,
        description="**Mandatory.** Which product inside the quote's `responses` to charge for.",
        examples=["ACME-EW-STD"],
    )
    optionref: int = Field(
        ...,
        description=(
            "**Mandatory.** **Zero-based index** into that product's `options` array — the "
            "position, not the cover term. The first option is `0`. Out of range returns `400`."
        ),
        examples=[0],
    )
    email: Optional[str] = Field(
        None,
        description="Customer email, used to pre-fill the Stripe Checkout page.",
        examples=["jane.okafor@example.com"],
    )
    product_name: Optional[str] = Field(
        None,
        description="Display name override. Defaults to the quote's `product_id`, which is rarely customer-friendly — worth setting.",
        examples=["Acme Extended Warranty — 24 months"],
    )
    product_description: Optional[str] = Field(
        None,
        description="Description shown on the Checkout page.",
        examples=["Parts and labour cover for your dishwasher"],
    )
    product_images: Optional[List[str]] = Field(
        None,
        description="Absolute image URLs for the Checkout page.",
        examples=[["https://cdn.example.com/images/cover.png"]],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "quote_id": "68a1c2d3e4b21d0f8c9e7712",
                "product_id": "ACME-EW-STD",
                "optionref": 0,
                "email": "jane.okafor@example.com",
                "product_name": "Acme Extended Warranty — 24 months",
                "product_description": "Parts and labour cover for your dishwasher",
            }
        }
    }


@router.post(
    "/generate_payment_link",
    summary="Create a payment link for one option of a stored quote",
    response_description="The same payload as `/generate_checkout_session`: URLs, session id and pre-fill diagnostics.",
    responses={
        200: json_response(
            "The Stripe session was created for the selected option.",
            {
                "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_a1B2c3D4e5F6",
                "checkout_url_short": "https://tinyurl.com/2p8kd4xr",
                "session_id": "cs_test_a1B2c3D4e5F6",
                "expires_at": 1786000000,
                "status": "open",
                "customer": None,
                "prefill_phone": None,
                "prefill_email": "jane.okafor@example.com",
            },
        ),
        400: error(
            "`quote_id` is malformed, `optionref` is out of range, or the chosen option carries "
            "no usable `rounded_price_pence` (which is the case for options that failed rating).",
            "Optionref out of range for this product",
        ),
        404: error("No such quote, or that `product_id` is not in the quote.", "Product not found in quote responses"),
        500: error("Stripe session creation failed.", "Internal error during Stripe session creation: ..."),
    },
)
def generate_quote_payment_link(req: PaymentLinkRequest):
    """
    Turn one option of a **stored quote** into a Stripe payment link.

    Unlike `POST /generate_checkout_session`, the amount is not supplied by the caller: it is
    read from the quote (`rounded_price_pence` on the chosen option), so the customer is charged
    exactly what was quoted. Currency and Stripe mode come from the quote too.

    **Selecting the option.** `product_id` picks the entry in the quote's `responses`; `optionref`
    is the **zero-based array index** within that entry's `options` — `0` for the first term
    offered, not the number of months. Fetch the quote with `GET /quote/{quote_id}` first if you
    need to see the order.

    Only priced options can be charged: an option that failed rating has no
    `rounded_price_pence` and returns `400`.

    The quote id is passed to Stripe as `internal_reference`, and the client, source, quote,
    product and option index are attached as metadata, so a completed payment can be reconciled
    from the webhook alone.

    **This endpoint is not authenticated** — call it server-side, not from a browser. The
    response is whatever `/generate_checkout_session` returns, including the shortened link.
    """
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
        cancel_url="https://frontend-production-7798.up.railway.app/lookup",
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
