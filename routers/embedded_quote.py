from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional
from utils.mongo import require_client
from bson import ObjectId
import os

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

# Import the existing assignment and rating logic
from .assign_product_by_device_id import assign_product_for_device
from .rate_request import (
    RateRequest as RateReqModel,
    price_and_group,
    require_regional_postcode,
    store_quote,
)

router = APIRouter(tags=["Quotes"])


class EmbeddedQuoteRequest(BaseModel):
    """The device to quote for."""

    device_id: str = Field(
        ...,
        description="**Mandatory.** Id of an already-registered device, as returned by `POST /device-register`.",
        examples=["6820f1c9a4b21d0f8c9e4471"],
    )
    clientKey: Optional[str] = Field(
        None,
        description=(
            "Tenant key to record on the quote. When omitted it falls back to the device's "
            "`registrationParameters.clientRef`, which may not be a ClientKey at all — pass it "
            "explicitly if the quote needs to be attributed reliably."
        ),
        examples=["acme_uk_live"],
    )

    device_working: Optional[bool] = Field(
        None,
        description=(
            "Whether the device is in working order right now. Omitted or `null` falls back "
            "to the device's stored `registrationParameters.deviceWorking`, and that in turn "
            "reads as working when it is unset — so only a faulty device has to say so. "
            "`false` matches the client's `when.deviceWorking: false` assignment rules, which "
            "is how a broken device is offered repair cover rather than a warranty."
        ),
        examples=[True],
    )

    postcode: Optional[str] = Field(
        None,
        max_length=16,
        description=(
            "Where the device is, for products whose assignment rule sets "
            "`when.regionalPricing: true`. Required for those — the call returns `422` with "
            "`detail.code: \"postcode_required\"` without it — and ignored otherwise. Stored "
            "on the quote, normalised, so the repair can be checked against it later."
        ),
        examples=["IV1 1HY"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "clientKey": "acme_uk_live",
                "device_working": True,
                "postcode": None,
            }
        }
    }


@router.post(
    "/embedded_quote",
    summary="Quote a registered device in one call (assign, then rate)",
    response_description="The stored quote id, the priced options, and the assignment behind them.",
    responses=secured({
        200: json_response(
            "The device was assigned products and those were priced.",
            {
                "quote_id": "68a1c2d3e4b21d0f8c9e7712",
                "responses": [
                    {
                        "product_id": "ACME-EW-STD",
                        "client": "ACME-UK",
                        "currency": "GBP",
                        "locale": "en_GB",
                        "category": "Dishwasher",
                        "age": 15,
                        "price": 449.99,
                        "multi_count": 1,
                        "source": "web",
                        "options": [
                            {"status": "ok", "poc": 24, "mode": "live", "rate": 71.34, "rounded_price": 71.49, "rounded_price_pence": 7149},
                            {"status": "ok", "poc": 36, "mode": "live", "rate": 96.18, "rounded_price": 96.49, "rounded_price_pence": 9649},
                        ],
                    }
                ],
                "assignment": {
                    "Inputs": {"client": "ACME-UK", "source": "web", "category": "Dishwasher", "price": 449.99, "locale": "en_GB", "currency": "GBP"},
                    "DistinctProductIds": ["ACME-EW-STD"],
                },
            },
        ),
        400: error("The device id is malformed, or the stored device is missing a required field.", "Device 'currency' is missing or blank."),
        404: error("The device does not exist, or nothing was assignable to it.", "No products assigned for device"),
        422: error(
            "The assigned products are regionally priced and `postcode` is missing (`detail.code: postcode_required`) "
            "or not valid for the device's country (`postcode_invalid`). Nothing was priced or stored.",
            {"code": "postcode_required", "message": "A postcode is required to price these products."},
        ),
        500: error("Assignment or rating failed unexpectedly.", "Rate request error: ..."),
    }),
)
async def embedded_quote(payload: EmbeddedQuoteRequest, _: None = Depends(verify_token)):
    """
    Quote a registered device in a single call — the convenience wrapper used by embedded
    journeys.

    It chains the two steps you would otherwise make yourself:

    1. `GET /assign_product_for_device/{device_id}` — work out which products and terms the
       device qualifies for, reading everything from the stored device.
    2. `POST /rate_request` — price every one of them and persist the result as a quote.

    You get back the `quote_id`, the grouped priced `responses`, and the `assignment` those
    prices came from, so a UI can render options without a second round trip.

    **Errors propagate from the underlying steps** — a device that exists but qualifies for
    nothing returns `404`, and a device missing `client`, `source`, `locale` or `currency`
    returns `400`. Unlike `/rate_request`, which reports per-line failures inside a `200`, this
    endpoint has nothing to return if assignment itself fails.

    Only `device_id` is mandatory. `device_working` is the one input the stored device may not
    know: send `false` when the customer is quoting a device that is already broken, and the
    assignment matches the client's faulty-device rules instead of the working-device ones.
    """
    device_id = payload.device_id

    # 1) Run product assignment for the device (this will raise HTTPException on failure)
    try:
        assignment_result = assign_product_for_device(device_id, device_working=payload.device_working)
    except HTTPException:
        # Re-raise to propagate proper status
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assignment error: {str(e)}")

    products = assignment_result.get("Products") or []
    if not products:
        raise HTTPException(status_code=404, detail="No products assigned for device")

    # 2) Regional pricing is the rule's decision. If it applies, the postcode has to be here
    # before anything is priced — a quote stored without it could not be checked against
    # where the repair actually happens.
    regional = any(p.get("regional_pricing") for p in products)
    postcode = require_regional_postcode(payload.postcode, products[0].get("locale")) if regional else None

    # 3) Build rate requests from assigned products
    requests: List[RateReqModel] = []
    for p in products:
        try:
            rr = RateReqModel(
                product_id=p.get("product_id"),
                currency=p.get("currency"),
                locale=p.get("locale"),
                poc=int(p.get("poc") or 0),
                category=p.get("category"),
                age=int(p.get("age") or 0),
                price=float(p.get("price") or 0),
                multi_count=int(p.get("multi_count") or 0),
                client=p.get("client"),
                source=p.get("source"),
                mode=p.get("mode") or "live",
                regional_pricing=bool(p.get("regional_pricing")),
                postcode=postcode if p.get("regional_pricing") else None,
            )
            requests.append(rr)
        except Exception as e:
            # Skip malformed product entries but log / surface an error
            raise HTTPException(status_code=500, detail=f"Failed to build rate request for product: {str(e)}")

    # Determine clientKey: prefer explicit payload.clientKey, else fall back
    # to device.registrationParameters.clientRef (if available).
    client_key = getattr(payload, 'clientKey', None)
    if not client_key:
        try:
            mongo = require_client()
            db = mongo['Activlink']
            devices_col = db['Devices']
            try:
                dev = devices_col.find_one({'_id': ObjectId(device_id)})
                if dev:
                    client_key = dev.get('registrationParameters', {}).get('clientRef')
            except Exception:
                # If device_id is not a valid ObjectId or lookup fails, ignore and leave client_key None
                client_key = None
        except Exception:
            client_key = None

    # 4) Price and store, exactly as `/rate_request` does — called directly so the quote can
    # carry the postcode it was priced for. The basket compares that with the repair address.
    try:
        grouped, _bracket = price_and_group(requests)
        extra = {"postcode": postcode, "regionalPricing": True} if regional else None
        quote_id = store_quote(grouped, device_id=device_id, client_key=client_key, extra=extra)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rate request error: {str(e)}")

    return {
        "quote_id": quote_id,
        "responses": grouped,
        "assignment": assignment_result,
    }
