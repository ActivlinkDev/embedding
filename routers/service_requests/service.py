"""Storage, tenancy and state for repair service requests.

A ServiceRequest is what a customer creates when they report a fault on a device and book
an engineer: the fault they picked, any photos, and the appointment. Everything in the
`/service-requests` routers goes through this module, so the tenancy rules below are
written once.

TENANCY
-------
`client` holds the **`Client_ID`**, not the ClientKey, mirroring `Devices.client` — that
is what `utils.tenant` treats as the ownership boundary. It is always resolved from the
submitted ClientKey with `client_id_for_key`, never taken from the request, and **every**
read and write filters on it.

A miss is a **404, never a 403**. A request id belonging to another tenant has to be
indistinguishable from one that does not exist, or the error code itself becomes a way to
enumerate other tenants' records.

A service request can only be created against a device the tenant already owns, which is
what makes the filter meaningful: the id can never have pointed anywhere else.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

from utils.mongo import require_client
from utils.tenant import client_id_for_key

_client = require_client()
_db = _client["Activlink"]

service_requests_collection = _db["ServiceRequests"]
media_collection = _db["ServiceRequestMedia"]
devices_collection = _db["Devices"]
counters_collection = _db["Counters"]

# --- state ------------------------------------------------------------------------------

REPORTED = "REPORTED"
SCHEDULED = "SCHEDULED"
CONFIRMED = "CONFIRMED"
COMPLETED = "COMPLETED"
CANCELLED = "CANCELLED"

# Which statuses each status may move to. SCHEDULED -> SCHEDULED is a reschedule.
_TRANSITIONS: Dict[str, set] = {
    REPORTED: {SCHEDULED, CANCELLED},
    SCHEDULED: {SCHEDULED, CONFIRMED, CANCELLED},
    CONFIRMED: {COMPLETED, CANCELLED},
    COMPLETED: set(),
    CANCELLED: set(),
}

# Statuses past the point where an appointment can still be chosen. A list, not a set, so
# the `$nin` it builds is ordered the same way on every call.
_UNSCHEDULABLE = [COMPLETED, CANCELLED]

MAX_MEDIA_PER_REQUEST = 4


def can_transition(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, set())


def now() -> datetime:
    return datetime.now(timezone.utc)


def event(type_: str, payload: Optional[dict] = None, actor: str = "customer") -> dict:
    """An audit entry, shaped like the one `routers/contract/contract_service.py` writes."""
    return {"type": type_, "payload": payload or {}, "actor": actor, "at": now()}


def next_reference() -> str:
    """Atomic, monotonic customer-facing reference: SRV-<year>-000123."""
    year = now().year
    doc = counters_collection.find_one_and_update(
        {"_id": f"service_request-{year}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    seq = doc["seq"] if doc and "seq" in doc else 1
    return f"SRV-{year}-{seq:06d}"


# --- tenancy ----------------------------------------------------------------------------

def resolve_client_id(clientkey: str) -> str:
    """The `Client_ID` for a ClientKey, or 400.

    Never returns a falsy id: an unknown key must fail the request rather than fall
    through into a query with no tenant filter. Mirrors
    `routers/registration_overview.py:resolve_client_id`.
    """
    client_id = client_id_for_key(clientkey)
    if not client_id:
        raise HTTPException(400, "Invalid clientkey.")
    return client_id


def object_id(value: str, what: str) -> ObjectId:
    if not value or not ObjectId.is_valid(value):
        raise HTTPException(400, f"Invalid {what}.")
    return ObjectId(value)


def owned_device(device_id: str, client_id: str) -> dict:
    """The device, if this tenant owns it. 404 otherwise — see the module docstring."""
    device = devices_collection.find_one(
        {"_id": object_id(device_id, "deviceId"), "client": client_id})
    if not device:
        raise HTTPException(404, "Device not found")
    return device


def owned_request(service_request_id: str, client_id: str) -> dict:
    """The service request, if this tenant owns it. 404 otherwise."""
    doc = service_requests_collection.find_one(
        {"_id": object_id(service_request_id, "serviceRequestId"), "client": client_id})
    if not doc:
        raise HTTPException(404, "Service request not found")
    return doc


# --- serialisation ----------------------------------------------------------------------

def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def serialize_media(doc: dict) -> dict:
    """Media metadata only. The `data` Binary is never returned through a JSON body."""
    return {
        "mediaId": str(doc["_id"]),
        "name": doc.get("name"),
        "contentType": doc.get("contentType"),
        "size": doc.get("size"),
        "uploadedAt": _iso(doc.get("uploadedAt")),
    }


def media_for(service_request_id: ObjectId, client_id: str) -> List[dict]:
    """This request's media metadata, tenant-filtered, with the bytes projected away."""
    cursor = media_collection.find(
        {"serviceRequestId": service_request_id, "client": client_id},
        {"data": 0},
    ).sort("uploadedAt", 1)
    return [serialize_media(doc) for doc in cursor]


def serialize_request(doc: dict, include_media: bool = True) -> dict:
    """The customer-facing view of a service request."""
    payload = {
        "serviceRequestId": str(doc["_id"]),
        "reference": doc.get("reference"),
        "status": doc.get("status"),
        "deviceId": doc.get("deviceId"),
        "locale": doc.get("locale"),
        "category": doc.get("category"),
        "fault": doc.get("fault") or {},
        "appointment": doc.get("appointment"),
        "quote_id": doc.get("quote_id"),
        "basket_id": doc.get("basket_id"),
        "contract_reference": doc.get("contract_reference"),
        "mediaCount": doc.get("mediaCount", 0),
        "createdAt": _iso(doc.get("createdAt")),
        "updatedAt": _iso(doc.get("updatedAt")),
    }
    if include_media:
        payload["media"] = media_for(doc["_id"], doc["client"])
    return payload


# --- indexes ----------------------------------------------------------------------------

def ensure_service_request_indexes() -> None:
    """Indexes for both collections plus the fault catalogue. Safe to call repeatedly."""
    service_requests_collection.create_index([("client", 1), ("createdAt", -1)])
    service_requests_collection.create_index([("client", 1), ("deviceId", 1)])
    service_requests_collection.create_index("reference", unique=True)
    service_requests_collection.create_index("basket_id", sparse=True)
    service_requests_collection.create_index([("client", 1), ("appointment.date", 1)], sparse=True)
    media_collection.create_index([("serviceRequestId", 1), ("uploadedAt", 1)])
    media_collection.create_index([("client", 1), ("serviceRequestId", 1)])

    from routers.faults.catalogue import ensure_fault_catalogue_indexes
    ensure_fault_catalogue_indexes()


# --- checkout hook ----------------------------------------------------------------------

def attach_contract_to_service_request(
    service_request_id: Optional[str],
    contract_reference: str,
    order_reference: str,
    basket_id: Optional[str],
) -> bool:
    """Mark a service request confirmed once its contract is issued.

    Called from contract issuance, which runs inside the Stripe webhook and therefore has
    no ClientKey context — so this filters on `_id` alone. That is safe because the id
    reached the basket line through a tenant-scoped endpoint and was never caller-chosen
    against another tenant's record.

    Returns whether a document was updated. Callers treat a False as unremarkable: a
    basket line with no service request is the ordinary cover journey.
    """
    if not service_request_id or not ObjectId.is_valid(service_request_id):
        return False
    result = service_requests_collection.update_one(
        {"_id": ObjectId(service_request_id), "status": {"$nin": _UNSCHEDULABLE}},
        {
            "$set": {
                "status": CONFIRMED,
                "contract_reference": contract_reference,
                "basket_id": basket_id,
                "updatedAt": now(),
            },
            "$push": {"events": event(
                "CONFIRMED",
                {"contract_reference": contract_reference, "order_reference": order_reference},
                actor="system",
            )},
        },
    )
    return result.matched_count > 0
