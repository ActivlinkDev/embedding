from fastapi import APIRouter, HTTPException, Depends
from pydantic import AliasChoices, BaseModel, Field, EmailStr
from typing import Optional, Dict, Any, List
from bson import ObjectId
from pymongo import MongoClient
import os

from utils.api_docs import error, json_response, secured
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
    """Checkout details for a basket. Only `basket_id` is mandatory — everything else overrides
    a default derived from the basket."""

    basket_id: str = Field(
        ...,
        description="**Mandatory.** The basket to charge for. Must contain at least one line.",
        examples=["68b2d1f0a4b21d0f8c9e8801"],
    )
    email: Optional[EmailStr] = Field(
        None,
        validation_alias=AliasChoices("email", "customerEmail"),
        description="Customer email, used to pre-fill Stripe Checkout. Accepted as `email` or `customerEmail`; must be valid if sent.",
        examples=["jane.okafor@example.com"],
    )
    customer_phone: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("customerPhone", "customer_phone", "phone"),
        description=(
            "E.164 phone number used to pre-fill the Stripe phone field. Accepted as "
            "`customerPhone`, `customer_phone` or `phone`."
        ),
        examples=["+447700900123"],
    )
    product_name: Optional[str] = Field(
        None,
        description="Line description on the Checkout page. Defaults to the winning discount rule's name, else `Basket checkout`.",
        examples=["Acme device protection — 2 items"],
    )
    product_description: Optional[str] = Field(
        None,
        description="Sub-heading on the Checkout page. Defaults to `Basket items checkout`.",
        examples=["Cover for your dishwasher and washing machine"],
    )
    product_images: Optional[List[str]] = Field(
        None,
        description="Images for the Checkout page. Defaults to images already on the basket lines.",
        examples=[["https://cdn.example.com/images/cover.png"]],
    )
    success_url: Optional[str] = Field(
        None,
        description="Where Stripe sends the customer after payment. **Set this** — the fallback is a placeholder domain.",
        examples=["https://shop.example.com/cover/success"],
    )
    cancel_url: Optional[str] = Field(
        None,
        description="Where Stripe sends the customer if they abandon checkout.",
        examples=["https://shop.example.com/cover/cancel"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "basket_id": "68b2d1f0a4b21d0f8c9e8801",
                "email": "jane.okafor@example.com",
                "customerPhone": "+447700900123",
                "product_name": "Acme device protection — 2 items",
                "success_url": "https://shop.example.com/cover/success",
                "cancel_url": "https://shop.example.com/cover/cancel",
            }
        }
    }


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


def _collect_product_images(items: list[dict[str, Any]], limit: int = 6) -> List[str]:
    seen = set()
    out: List[str] = []
    for it in items:
        imgs = (it or {}).get("product_images") or []
        if isinstance(imgs, list):
            for url in imgs:
                if not isinstance(url, str):
                    continue
                u = url.strip()
                if not u or u in seen:
                    continue
                seen.add(u)
                out.append(u)
                if len(out) >= limit:
                    return out
    return out


@router.post(
    "/basket/payment/create",
    summary="Create a Stripe checkout session for a whole basket",
    response_description="The same payload as `/generate_checkout_session`: URLs, session id and pre-fill diagnostics.",
    responses=secured({
        200: json_response(
            "The session was created for the basket total.",
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
        400: error(
            "`basket_id` is malformed, the basket has no lines, or no total could be determined "
            "from it.",
            "Basket is empty",
        ),
        404: error("No basket with this id.", "Basket not found"),
        500: error("Stripe session creation failed.", "Internal error during Stripe session creation: ..."),
    }),
)
def create_basket_payment_session(req: BasketPaymentRequest, _: None = Depends(verify_token)):
    """
    Charge a whole basket in **one** Stripe Checkout session — a single line item for the basket
    total, not one per device.

    **The amount comes from the basket**, in this order: `final_total` (discount applied), then
    `subtotal`, then the sum of the lines' `rounded_price_pence`. The caller cannot set it. If
    none of those yields a positive figure the request is rejected with `400`, so rate the basket
    before checkout if the totals may be stale.

    Currency, locale and Stripe mode are taken from the basket lines, defaulting to `gbp` and
    `payment`. The basket id is passed as `internal_reference` and repeated in the metadata
    alongside client and source, so the webhook can reconcile the payment to the basket.

    `success_url` and `cancel_url` **should be supplied** — the built-in fallbacks are
    placeholders and will send customers somewhere unhelpful.

    The response is whatever `/generate_checkout_session` returns, including the shortened link
    and the pre-fill diagnostics.
    """
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
    # Prefer provided images; else gather from basket items if present
    product_images = req.product_images or _collect_product_images(items)
    success_url = req.success_url or "https://yourdomain.com/success"
    cancel_url = req.cancel_url or "https://frontend-production-7798.up.railway.app/lookup"

    req_checkout = CheckoutSessionRequest(
        product_name=product_name,
        product_description=product_description,
        product_images=product_images,
        unit_amount=int(amount_minor),
        currency=currency,
        quantity=1,
        mode=mode_enum,
        success_url=success_url,
        cancel_url=cancel_url,
        locale=locale,
        internal_reference=str(basket["_id"]),
        metadata={
            "basket_id": str(basket["_id"]),
            "client": _extract_client(items),
            "source": _extract_source(items),
        },
        customer_email=req.email if req.email else None,
        customer_phone=req.customer_phone if req.customer_phone else None,
    )

    # 5) Create session via shared helper
    try:
        return generate_checkout_session(req_checkout)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error during Stripe session creation: {e}")
