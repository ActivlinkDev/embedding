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

# Fix 6: guard against missing MONGO_URI
_mongo_uri = os.getenv("MONGO_URI")
if not _mongo_uri:
    raise RuntimeError("MONGO_URI not set in environment.")

_client = MongoClient(_mongo_uri)
_db = _client["Activlink"]
qr_collection = _db["QR_Collection"]
clientkey_collection = _db["ClientKey"]
customsku_collection = _db["CustomSKU"]

try:
    qr_collection.create_index("hex_key", unique=True)
    qr_collection.create_index("batch_id")
    qr_collection.create_index("client_key")
    qr_collection.create_index("custom_sku", sparse=True)
    # Fix 1: partial index so null/absent device_id is never indexed
    qr_collection.create_index(
        "device_id",
        unique=True,
        partialFilterExpression={"device_id": {"$exists": True}},
    )
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


def _unique_hex_key(used_in_batch: set, max_attempts: int = 20) -> str:
    """Generate a hex key unique both in the DB and within the current batch (Fix 2)."""
    for _ in range(max_attempts):
        key = _generate_hex_key()
        if key not in used_in_batch and not qr_collection.find_one({"hex_key": key}):
            return key
    raise HTTPException(status_code=503, detail="Unable to generate unique hex key; try again.")


@router.post("/qr-collection/generate")
def generate_qr_collection(
    body: GenerateQRRequest,
    _: None = Depends(verify_token),
):
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

    batch_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    docs = []
    qr_codes = []
    used_in_batch: set = set()

    for _ in range(body.count):
        hex_key = _unique_hex_key(used_in_batch)
        used_in_batch.add(hex_key)
        scan_url = f"{FASTAPI_BASE_URL}/qr/scan/{hex_key}"
        qr_image = generate_qr_code(scan_url)
        # Fix 1: omit device_id and paired_at fields entirely (don't store null)
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
            "created_at": now,
            "created_by": body.created_by,
        }
        docs.append(doc)
        qr_codes.append({"hex_key": hex_key, "scan_url": scan_url, "qr_image_b64": qr_image})

    try:
        # Fix 2: ordered=True so any rare DB-level collision stops cleanly (no partial writes)
        qr_collection.insert_many(docs, ordered=True)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Duplicate hex key; please retry.")

    return {
        "batch_id": batch_id,
        "count": body.count,
        "client_key": body.client_key,
        "custom_sku": body.custom_sku,
        "qr_codes": qr_codes,
    }
