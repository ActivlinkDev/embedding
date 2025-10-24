from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional

from utils.dependencies import verify_token

# Import the existing assignment and rating logic
from .assign_product_by_device_id import assign_product_for_device
from .rate_request import RateRequest as RateReqModel, RateRequestBatch, rate_request

router = APIRouter(tags=["Embedded Quote"])


class EmbeddedQuoteRequest(BaseModel):
    device_id: str = Field(..., example="64f7a1e4b9c1f2a3d4e5f6a7")
    clientKey: Optional[str] = Field(None, description="Optional clientKey to pass through to rate_request")


@router.post("/embedded_quote")
async def embedded_quote(payload: EmbeddedQuoteRequest, _: None = Depends(verify_token)):
    """Create a quote for a device by chaining product assignment and rate request.

    Input: { device_id }
    Output: { quote_id }
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

    # Use clientKey from request payload if provided; otherwise leave None
    batch = RateRequestBatch(deviceId=device_id, clientKey=getattr(payload, 'clientKey', None), requests=requests)

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
