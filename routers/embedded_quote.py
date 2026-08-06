from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional
from pymongo import MongoClient
from bson import ObjectId
import os

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

# Import the existing assignment and rating logic
from .assign_product_by_device_id import assign_product_for_device
from .rate_request import RateRequest as RateReqModel, RateRequestBatch, rate_request

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

    model_config = {
        "json_schema_extra": {
            "example": {"device_id": "6820f1c9a4b21d0f8c9e4471", "clientKey": "acme_uk_live"}
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

    Only `device_id` is mandatory.
    """
    device_id = payload.device_id

    # 1) Run product assignment for the device (this will raise HTTPException on failure)
    try:
        assignment_result = assign_product_for_device(device_id)
    except HTTPException:
        # Re-raise to propagate proper status
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assignment error: {str(e)}")

    products = assignment_result.get("Products") or []
    if not products:
        raise HTTPException(status_code=404, detail="No products assigned for device")

    # 2) Build rate requests from assigned products
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
            mongo = MongoClient(os.getenv('MONGO_URI'))
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

    batch = RateRequestBatch(deviceId=device_id, clientKey=client_key, requests=requests)

    # 3) Call the rate_request logic which will store quotes and return quote_id
    try:
        rate_resp = rate_request(batch)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rate request error: {str(e)}")

    # Return quote id and the grouped rate responses produced by rate_request.
    return {
        "quote_id": rate_resp.get("quote_id"),
        "responses": rate_resp.get("responses"),
        "assignment": assignment_result,
    }
