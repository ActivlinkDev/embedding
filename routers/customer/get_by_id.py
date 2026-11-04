from fastapi import APIRouter, Query, HTTPException, Depends
from bson import ObjectId
import os

from utils.api_docs import error, json_response, secured
from utils.dependencies import caller_client_key
from utils.mongo import require_client
from utils.tenant import client_id_for_key, device_ids_owned_by

router = APIRouter(tags=["Customers"], prefix="")

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI not set in environment")

client = require_client()
db = client["Activlink"]
customer_collection = db["Customer"]
orders_collection = db["ContractOrders"]


def _serialize_doc(doc: dict) -> dict:
    # Recursively serialize a document, converting ObjectId to str anywhere in the structure.
    from bson import ObjectId as _ObjectId

    def _serialize_value(value):
        if isinstance(value, _ObjectId):
            return str(value)
        if isinstance(value, dict):
            return {k: _serialize_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_serialize_value(v) for v in value]
        # leave other types (datetime, numbers, strings) as-is
        return value

    if not doc:
        return {}
    return _serialize_value(doc)


def _restrict_to_tenant(doc: dict, client_key: str) -> dict | None:
    """The customer as `client_key` may see it, or None when that tenant has no claim on them.

    Fail-closed: an unknown ClientKey, or a customer with no device or order tied to the
    tenant, yields None rather than the unfiltered record.
    """
    client_id = client_id_for_key(client_key)
    if not client_id:
        return None

    devices = doc.get("devices") if isinstance(doc.get("devices"), list) else []
    device_ids = [str(d.get("deviceId")) for d in devices if isinstance(d, dict) and d.get("deviceId")]
    owned = device_ids_owned_by(client_id, device_ids) if device_ids else set()

    prefs = doc.get("marketingPreferences") if isinstance(doc.get("marketingPreferences"), list) else []
    own_prefs = [p for p in prefs if isinstance(p, dict) and p.get("clientKey") == client_key]

    has_order = bool(orders_collection.find_one(
        {"customer_id": str(doc.get("_id")), "client_key": client_key}, {"_id": 1}))

    # Consent is deliberately not a qualifying relationship: /get-or-create-customer is
    # unauthenticated and records marketingPreferences for any known ClientKey, so anyone could
    # manufacture it. Only devices and orders, written by authenticated/trusted paths, count.
    if not owned and not has_order:
        return None

    scoped = dict(doc)
    if "devices" in doc:
        scoped["devices"] = [d for d in devices if isinstance(d, dict) and str(d.get("deviceId")) in owned]
    if "marketingPreferences" in doc:
        scoped["marketingPreferences"] = own_prefs
    return scoped


@router.get(
    "/customer/by-id",
    summary="Fetch a customer record",
    response_description="The customer document, wrapped in `data`, without its transaction log.",
    responses=secured({
        200: json_response(
            "The customer was found.",
            {
                "data": {
                    "_id": "6820f1c9a4b21d0f8c9e9001",
                    "name": "Jane Okafor",
                    "telephone": "+447700900123",
                    "email": "jane.okafor@example.com",
                    "devices": [
                        {"deviceId": "6820f1c9a4b21d0f8c9e4471", "status": "contract"},
                        {"deviceId": "6820f1c9a4b21d0f8c9e4472", "status": "registered"},
                    ],
                }
            },
        ),
        400: error("`customer_id` is not a valid 24-character ObjectId.", "Invalid customer_id"),
        404: error("No customer with this id.", "Customer not found"),
        500: error("The lookup failed unexpectedly.", "Internal error: ..."),
    }),
)
def get_customer_by_id(
    customer_id: str = Query(
        ...,
        alias="customer_id",
        description="**Mandatory.** The customer's `_id` (24-character ObjectId).",
        examples=["6820f1c9a4b21d0f8c9e9001"],
    ),
    scope: str | None = Depends(caller_client_key),
):
    """
    Fetch a customer record by id.

    The document is returned as stored, with every nested `ObjectId` converted to a string —
    **except `transaction_log`, which is always stripped** from the response. Do not rely on this
    endpoint for payment history; use `GET /customers/{customer_id}/orders`.

    `devices` carries the pairing produced by `POST /pair-customer`: `contract` for devices with
    cover, `registered` for devices where it was declined.

    `customer_id` is mandatory.

    **Tenant scoping.** A customer record is shared between clients, so a caller pinned to a
    ClientKey only sees a customer it has a relationship with: a device in `devices` owned by
    its client, or an order placed under its ClientKey. Anyone else gets `404`, as if the id
    did not exist. Consent in `marketingPreferences` does **not** count, because
    `/get-or-create-customer` lets anyone record it. For such a caller `devices` and
    `marketingPreferences` are narrowed to its own entries.
    """
    try:
        try:
            objid = ObjectId(customer_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid customer_id")

        doc = customer_collection.find_one({"_id": objid})
        if not doc:
            raise HTTPException(status_code=404, detail="Customer not found")

        if scope:
            doc = _restrict_to_tenant(doc, scope)
            if doc is None:
                # 404 rather than 403: a pinned caller must not learn another tenant's ids exist.
                raise HTTPException(status_code=404, detail="Customer not found")

        # Exclude transaction_log from API responses
        if isinstance(doc, dict) and "transaction_log" in doc:
            try:
                del doc["transaction_log"]
            except Exception:
                # ignore if deletion fails for any reason
                pass

        return {"data": _serialize_doc(doc)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {type(e).__name__}")
