from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from bson import ObjectId
import os
import uuid
from datetime import datetime, timezone

from utils.dependencies import verify_token
from routers.embedded_register_device import generate_qr_code
from routers.qr.generate_qr_collection import qr_collection, clientkey_collection, customsku_collection, _unique_hex_key

router = APIRouter(tags=["QR"])

FASTAPI_BASE_URL = os.getenv("FASTAPI_BASE_URL", "").rstrip("/")


class DeviceQRRequest(BaseModel):
    client_key: str
    custom_sku: Optional[str] = None
    serial: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    gtin: Optional[str] = None
    created_by: Optional[str] = None


@router.post("/qr/device-generate")
def generate_device_qr(
    body: DeviceQRRequest,
    _: None = Depends(verify_token),
):
    """Generate a single QR code for an IoT device. The QR encodes a redirect
    URL that pre-populates the registration form with the device's identifiers."""
    client_doc = clientkey_collection.find_one({"ClientKey": body.client_key})
    if not client_doc:
        raise HTTPException(status_code=400, detail="Invalid client_key")

    if body.custom_sku:
        try:
            sku_oid = ObjectId(body.custom_sku)
        except Exception:
            raise HTTPException(status_code=400, detail="custom_sku must be a valid MongoDB ObjectId")
        # Fix 3: validate SKU belongs to the requesting client
        client_id = client_doc.get("Client_ID")
        if not customsku_collection.find_one({"_id": sku_oid, "Client": client_id}):
            raise HTTPException(status_code=400, detail="custom_sku not found for this client")

    hex_key = _unique_hex_key(used_in_batch=set())
    scan_url = f"{FASTAPI_BASE_URL}/qr/scan/{hex_key}"
    qr_image = generate_qr_code(scan_url)
    now = datetime.now(timezone.utc)

    device_params = {
        "serial": body.serial or None,
        "make": body.make or None,
        "model": body.model or None,
        "gtin": body.gtin or None,
    }

    # Fix 1: omit device_id and paired_at fields entirely (don't store null)
    doc = {
        "hex_key": hex_key,
        "batch_id": str(uuid.uuid4()),
        "client_key": body.client_key,
        "custom_sku": body.custom_sku,
        "device_params": device_params,
        "qr_image_b64": qr_image,
        "scan_url": scan_url,
        "status": "unscanned",
        "scan_count": 0,
        "scans": [],
        "created_at": now,
        "created_by": body.created_by,
    }
    qr_collection.insert_one(doc)

    return {
        "hex_key": hex_key,
        "scan_url": scan_url,
        "qr_image_b64": qr_image,
    }
