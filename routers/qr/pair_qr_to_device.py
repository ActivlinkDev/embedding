from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime, timezone

from utils.dependencies import verify_token
from routers.qr.generate_qr_collection import qr_collection

router = APIRouter(tags=["QR"])


class PairQRRequest(BaseModel):
    hex_key: str
    device_id: str


@router.post("/qr/pair")
def pair_qr_to_device(body: PairQRRequest, _: None = Depends(verify_token)):
    """Pair a QR code to a device registration. Idempotent if already paired
    to the same device_id; returns 409 if paired to a different device."""
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
    result = qr_collection.update_one(
        {"hex_key": hex_key, "device_id": None},
        {"$set": {"device_id": body.device_id, "paired_at": now, "status": "paired"}},
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=409, detail="QR code is already paired to a different device")

    return {
        "hex_key": hex_key,
        "device_id": body.device_id,
        "paired_at": now,
        "status": "paired",
    }
