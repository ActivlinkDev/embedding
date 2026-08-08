from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError
from datetime import datetime, timezone

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from routers.qr.generate_qr_collection import qr_collection

router = APIRouter(tags=["QR"])


class PairQRRequest(BaseModel):
    """Which code to bind to which device. Both fields are mandatory."""

    hex_key: str = Field(
        ...,
        description="**Mandatory.** The code's five-character hex key. Matched case-insensitively.",
        examples=["3F9A1"],
    )
    device_id: str = Field(
        ...,
        description="**Mandatory.** The registered device to bind it to.",
        examples=["6820f1c9a4b21d0f8c9e4471"],
    )

    model_config = {"json_schema_extra": {"example": {"hex_key": "3F9A1", "device_id": "6820f1c9a4b21d0f8c9e4471"}}}


@router.post(
    "/qr/pair",
    summary="Bind a QR code to a registered device",
    response_description="The pairing, including when it was made.",
    responses=secured({
        200: json_response(
            "The code is now bound to this device — or already was.",
            {
                "hex_key": "3F9A1",
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "paired_at": "2026-08-06T10:20:00+00:00",
                "status": "paired",
            },
        ),
        404: error("No QR code with this hex key.", "QR code not found"),
        409: error(
            "The code is already bound to a different device, or that device is already bound "
            "to another code. Pairing is one-to-one.",
            "QR code is already paired to a different device",
        ),
    }),
)
def pair_qr_to_device(body: PairQRRequest, _: None = Depends(verify_token)):
    """
    Bind a QR code to a registered device, so scanning it afterwards takes the customer straight
    to that device.

    **Pairing is one-to-one and permanent.** A code binds to exactly one device and a device to
    exactly one code; there is no unpair endpoint.

    **Repeating the same pairing is safe** — re-sending the same `hex_key` and `device_id`
    returns the existing pairing unchanged. Conflicts return `409`: either the code already
    points at a different device, or that device already has another code (the second case is
    reported as *"This device is already paired to another QR code"*).

    Both fields are mandatory. On success the code's status becomes `paired`.
    """
    hex_key = body.hex_key.upper()
    doc = qr_collection.find_one({"hex_key": hex_key})
    if not doc:
        raise HTTPException(status_code=404, detail="QR code not found")

    if doc.get("device_id") == body.device_id:
        return {
            "hex_key": hex_key,
            "device_id": doc["device_id"],
            "paired_at": doc.get("paired_at"),
            "status": doc.get("status"),
        }

    if doc.get("device_id") and doc.get("device_id") != body.device_id:
        raise HTTPException(status_code=409, detail="QR code is already paired to a different device")

    now = datetime.now(timezone.utc).isoformat()
    try:
        # Fix 1: match on device_id absent (not explicitly None) to align with partial index
        result = qr_collection.update_one(
            {"hex_key": hex_key, "device_id": {"$exists": False}},
            {"$set": {"device_id": body.device_id, "paired_at": now, "status": "paired"}},
        )
    except DuplicateKeyError:
        # Fix 11: race condition — another request paired this device_id first
        raise HTTPException(status_code=409, detail="This device is already paired to another QR code")

    if result.modified_count == 0:
        raise HTTPException(status_code=409, detail="QR code is already paired to a different device")

    return {
        "hex_key": hex_key,
        "device_id": body.device_id,
        "paired_at": now,
        "status": "paired",
    }
