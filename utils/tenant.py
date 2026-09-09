"""Tenant resolution helpers shared by customer-facing, phone-verified routes.

Customer-facing journeys (`/my-registrations`, the customer hub's contracts tab)
authenticate the *person* with an OTP, not the tenant: the trusted frontend proves
"this browser controls this phone number", and nothing in that proof says which
client's storefront the customer is standing in. Those routes therefore have to be
told the tenant separately, and the frontend derives it from the request host
(`beko.registermyproduct.io`) rather than from anything the browser can set.

The `Devices` collection is the authority on tenancy: every device document stores
``client`` — the ``Client_ID`` ("AO") resolved at registration time. A ``Client_ID``
owns several ``ClientKey`` records, one per channel/source (``AO12345`` for web,
``AOPON12345`` for POS), so resolving through ``Client_ID`` puts the boundary at the
brand: a lookup on ao.registermyproduct.io shows everything owned by "AO" and nothing
owned by another client, whichever channel registered it.

Contracts are scoped through the same authority rather than their own ``client_key``
field: a contract belongs to the tenant that owns the device it covers.
"""
from typing import Iterable, Optional

from bson import ObjectId

from utils.mongo import require_client

_client = require_client()
_db = _client["Activlink"]
_clients_collection = _db["ClientKey"]
_devices_collection = _db["Devices"]


def client_id_for_key(client_key: str) -> Optional[str]:
    """The ``Client_ID`` owning ``client_key``, or None if the key is unknown.

    Returning None (rather than raising) lets callers decide the status code: an
    unknown key is a 400 on a route the frontend controls, but must never widen a
    query to every tenant.
    """
    if not client_key or not isinstance(client_key, str):
        return None
    doc = _clients_collection.find_one({"ClientKey": client_key}, {"Client_ID": 1})
    client_id = (doc or {}).get("Client_ID")
    return client_id if isinstance(client_id, str) and client_id else None


def device_ids_owned_by(client_id: str, device_ids: Iterable[str]) -> set[str]:
    """Of ``device_ids``, the subset whose device document belongs to ``client_id``.

    Returned as the original string ids so callers can filter records that reference
    devices by string. Ids that are malformed, unknown, or owned by another tenant are
    simply absent from the result — callers keep only what comes back, so anything the
    tenant cannot be shown to own is dropped rather than assumed safe.
    """
    if not client_id:
        return set()
    wanted = {value for value in device_ids if isinstance(value, str) and ObjectId.is_valid(value)}
    if not wanted:
        return set()
    docs = _devices_collection.find(
        {"_id": {"$in": [ObjectId(value) for value in wanted]}, "client": client_id},
        {"_id": 1})
    return {str(doc["_id"]) for doc in docs}
