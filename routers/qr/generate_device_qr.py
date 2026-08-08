from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional
from bson import ObjectId
import os
import uuid
from datetime import datetime, timezone

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from routers.embedded_register_device import generate_qr_code
from routers.qr.generate_qr_collection import qr_collection, clientkey_collection, customsku_collection, _unique_hex_key

router = APIRouter(tags=["QR"])

FASTAPI_BASE_URL = (os.getenv("FASTAPI_BASE_URL") or "https://api.activlink.io").rstrip("/")


class DeviceQRRequest(BaseModel):
    """One QR code carrying a specific device's identifiers.

    Only `client_key` is mandatory, but a code with no `custom_sku` and no identifiers has
    nothing to pre-fill and will land the customer on a blank start page — supply at least a
    `custom_sku`, or `make` and `model`, or a `gtin`.
    """

    client_key: str = Field(..., description="**Mandatory.** Tenant the code belongs to.", examples=["acme_uk_live"])
    custom_sku: Optional[str] = Field(
        None,
        description="CustomSKU to pre-select. Must belong to this client, otherwise `400`.",
        examples=["681aa2f1c4b21d0f8c9e0012"],
    )
    serial: Optional[str] = Field(None, description="Serial number, carried into the registration form.", examples=["SN-8841203"])
    make: Optional[str] = Field(None, description="Manufacturer. Used with `model` when there is no `custom_sku`.", examples=["Bosch"])
    model: Optional[str] = Field(None, description="Model designation. Used with `make`.", examples=["SMS6ZCI00G"])
    gtin: Optional[str] = Field(None, description="Barcode, used to identify the product when make/model are absent.", examples=["5011773057240"])
    created_by: Optional[str] = Field(None, description="Who generated the code. Stored for audit.", examples=["jane.okafor"])

    model_config = {
        "json_schema_extra": {
            "example": {
                "client_key": "acme_uk_live",
                "custom_sku": "681aa2f1c4b21d0f8c9e0012",
                "serial": "SN-8841203",
                "make": "Bosch",
                "model": "SMS6ZCI00G",
                "created_by": "jane.okafor",
            }
        }
    }


@router.post(
    "/qr/device-generate",
    summary="Generate one QR code carrying a device's identifiers",
    response_description="The new code: its hex key, scan URL, and printable image.",
    responses=secured({
        200: json_response(
            "The code was created.",
            {
                "hex_key": "3F9A1",
                "scan_url": "https://api.activlink.io/qr/scan/3F9A1",
                "qr_image_b64": "iVBORw0KGgoAAAANSUhEUgAA… (base64 PNG)",
            },
        ),
        400: error(
            "Unknown `client_key`, or a `custom_sku` that is malformed or does not belong to "
            "this client.",
            "custom_sku not found for this client",
        ),
        503: error("A unique hex key could not be found after 20 attempts. Retry.", "Unable to generate unique hex key; try again."),
    }),
)
def generate_device_qr(
    body: DeviceQRRequest,
    _: None = Depends(verify_token),
):
    """
    Generate a **single** QR code that carries one device's identifiers — for a label applied to
    a specific unit, rather than a generic batch.

    The identifiers you supply (`serial`, `make`, `model`, `gtin`, `custom_sku`) are stored with
    the code and turned into query parameters when it is scanned, so the customer lands on a
    registration form already filled in. Which page they reach depends on what is present: a
    `custom_sku` or make+model or gtin sends them to the product page, and a code with none of
    them falls back to a generic start page.

    Only `client_key` is mandatory, but supplying no identifiers defeats the point. The response
    carries a **base64-encoded PNG** ready to print.

    For blank codes in bulk, use `POST /qr-collection/generate`.
    """
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
