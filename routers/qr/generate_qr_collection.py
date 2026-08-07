from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
import os
import secrets
import uuid
from datetime import datetime, timezone

from utils.api_docs import error, json_response, secured
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

FASTAPI_BASE_URL = (os.getenv("FASTAPI_BASE_URL") or "https://api.activlink.io").rstrip("/")
MAX_BATCH = 500


class GenerateQRRequest(BaseModel):
    """A batch of blank QR codes to mint for a client."""

    count: int = Field(
        ...,
        description=f"**Mandatory.** How many codes to generate. Must be between 1 and {MAX_BATCH}.",
        examples=[50],
    )
    client_key: str = Field(
        ...,
        description="**Mandatory.** Tenant the codes belong to. Must be a known ClientKey.",
        examples=["acme_uk_live"],
    )
    custom_sku: Optional[str] = Field(
        None,
        description=(
            "Bind every code in the batch to one product. Must be a CustomSKU **belonging to "
            "this client**, otherwise `400`. Omit for generic codes."
        ),
        examples=["681aa2f1c4b21d0f8c9e0012"],
    )
    created_by: Optional[str] = Field(None, description="Who requested the batch. Stored for audit.", examples=["jane.okafor"])

    model_config = {
        "json_schema_extra": {
            "example": {
                "count": 50,
                "client_key": "acme_uk_live",
                "custom_sku": "681aa2f1c4b21d0f8c9e0012",
                "created_by": "jane.okafor",
            }
        }
    }

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


@router.post(
    "/qr-collection/generate",
    summary="Generate a batch of QR codes for a client",
    response_description="The batch id and every code generated, with its scan URL and image.",
    responses=secured({
        200: json_response(
            "The batch was created. Each entry carries a base64 PNG ready to print.",
            {
                "batch_id": "0f2c4d1e-8a7b-4c5d-9e3f-2a1b0c9d8e7f",
                "count": 2,
                "client_key": "acme_uk_live",
                "custom_sku": "681aa2f1c4b21d0f8c9e0012",
                "qr_codes": [
                    {"hex_key": "3F9A1", "scan_url": "https://api.activlink.io/qr/scan/3F9A1", "qr_image_b64": "iVBORw0KGgoAAAANSUhEUgAA… (base64 PNG)"},
                    {"hex_key": "B72C4", "scan_url": "https://api.activlink.io/qr/scan/B72C4", "qr_image_b64": "iVBORw0KGgoAAAANSUhEUgAA… (base64 PNG)"},
                ],
            },
        ),
        400: error(
            "Unknown `client_key`, or a `custom_sku` that is malformed or does not belong to "
            "this client.",
            "custom_sku not found for this client",
        ),
        409: error(
            "A hex key collided at the database level. Codes generated before the collision "
            "may already be stored — see the description.",
            "Duplicate hex key; please retry.",
        ),
        503: error("A unique hex key could not be found after 20 attempts. Retry.", "Unable to generate unique hex key; try again."),
    }),
)
def generate_qr_collection(
    body: GenerateQRRequest,
    _: None = Depends(verify_token),
):
    """
    Mint a batch of QR codes for a client — the codes printed on labels, packaging or leaflets
    that a customer scans to start registration.

    Each code gets a unique five-character hex key, a scan URL under `GET /qr/scan/{hex_key}`,
    and a **base64-encoded PNG** ready to print (`<img src="data:image/png;base64,…">`). They
    start `unscanned` and unpaired; scanning moves them to `scanned`, and
    `POST /qr/pair` binds one to a device.

    Between 1 and 500 codes per call. Bind the batch to a product with `custom_sku` — it must
    belong to this client — or leave it out for generic codes that resolve at scan time.

    **A `409` can leave part of the batch behind.** Keys are checked for uniqueness before the
    insert, but if a concurrent request claims one in between, the insert stops at the collision
    — and the codes written *before* it stay in the collection. They are valid, scannable codes
    that no caller was told about, filed under a `batch_id` never returned. Retrying mints a
    fresh batch rather than completing the old one, so after a `409` list the batch with
    `GET /qr-collection?batch_id=…` if you need to find or clean up the orphans.

    `batch_id` groups the codes for later filtering via `GET /qr-collection`.

    For a single code carrying a specific device's identifiers, use `POST /qr/device-generate`.
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
        # ordered=True stops at the first DB-level key collision, but does NOT roll back the
        # documents already inserted before it — a 409 here can leave a partial batch stored.
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
