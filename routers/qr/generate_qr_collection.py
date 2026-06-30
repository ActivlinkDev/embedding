from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, field_validator
from typing import Optional
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
import os
import secrets
import uuid
from datetime import datetime, timezone

from utils.dependencies import verify_token
from routers.embedded_register_device import generate_qr_code

router = APIRouter(tags=["QR"])

_client = MongoClient(os.getenv("MONGO_URI"))
_db = _client["Activlink"]
qr_collection = _db["QR_Collection"]
clientkey_collection = _db["ClientKey"]
customsku_collection = _db["CustomSKU"]

try:
    qr_collection.create_index("hex_key", unique=True)
    qr_collection.create_index("batch_id")
    qr_collection.create_index("client_key")
    qr_collection.create_index("custom_sku", sparse=True)
    qr_collection.create_index("device_id", unique=True, sparse=True)
    qr_collection.create_index("status")
except Exception:
    pass

FASTAPI_BASE_URL = os.getenv("FASTAPI_BASE_URL", "").rstrip("/")
MAX_BATCH = 500


class GenerateQRRequest(BaseModel):
    count: int
    client_key: str
    custom_sku: Optional[str] = None
    created_by: Optional[str] = None

    @field_validator("count")
    @classmethod
    def count_in_range(cls, v):
        if v < 1 or v > MAX_BATCH:
            raise ValueError(f"count must be between 1 and {MAX_BATCH}")
        return v


def _generate_hex_key() -> str:
    return secrets.token_hex(3)[:5].upper()


def _unique_hex_key(max_attempts: int = 20) -> str:
    for _ in range(max_attempts):
        key = _generate_hex_key()
        if not qr_collection.find_one({"hex_key": key}):
            return key
    raise HTTPException(status_code=503, detail="Unable to generate unique hex key; try again.")


@router.post("/qr-collection/generate")
def generate_qr_collection(
    body: GenerateQRRequest,
    _: None = Depends(verify_token),
):
    if not clientkey_collection.find_one({"ClientKey": body.client_key}):
        raise HTTPException(status_code=400, detail="Invalid client_key")

    if body.custom_sku:
        try:
            sku_oid = ObjectId(body.custom_sku)
        except Exception:
            raise HTTPException(status_code=400, detail="custom_sku must be a valid MongoDB ObjectId")
        if not customsku_collection.find_one({"_id": sku_oid}):
            raise HTTPException(status_code=400, detail="custom_sku not found")

    batch_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    docs = []
    qr_codes = []

    for _ in range(body.count):
        hex_key = _unique_hex_key()
        scan_url = f"{FASTAPI_BASE_URL}/qr/scan/{hex_key}"
        qr_image = generate_qr_code(scan_url)
        doc = {
            "hex_key": hex_key,
            "batch_id": batch_id,
            "client_key": body.client_key,
            "custom_sku": body.custom_sku,
            "device_params": None,
            "qr_image_b64": qr_image,
            "scan_url": scan_url,
            "status": "unscanned",
            "scan_count": 0,
            "scans": [],
            "device_id": None,
            "paired_at": None,
            "created_at": now,
            "created_by": body.created_by,
        }
        docs.append(doc)
        qr_codes.append({"hex_key": hex_key, "scan_url": scan_url, "qr_image_b64": qr_image})

    try:
        qr_collection.insert_many(docs, ordered=False)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Duplicate hex key generated; please retry.")

    return {
        "batch_id": batch_id,
        "count": body.count,
        "client_key": body.client_key,
        "custom_sku": body.custom_sku,
        "qr_codes": qr_codes,
    }
