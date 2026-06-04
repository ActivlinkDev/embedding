"""Contract administration endpoints (read + manage).

Issuance happens via the Stripe webhook; these endpoints power the customer-hub
'Contracts' tab and internal administration. All routes require the bearer token.
"""
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from utils.dependencies import verify_token

from routers.contract import contract_service as svc

router = APIRouter(tags=["Contracts"], dependencies=[Depends(verify_token)])


def _serialize(doc: dict) -> dict:
    if not doc:
        return doc
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    if doc.get("order_id") is not None:
        doc["order_id"] = str(doc["order_id"])
    return doc


def _get_or_404(contract_id: str) -> dict:
    try:
        oid = ObjectId(contract_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid contract id")
    doc = svc.contracts_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Contract not found")
    return doc


@router.get("/contracts")
def list_contracts(
    customer_id: str | None = None,
    device_id: str | None = None,
    client_key: str | None = None,
    status: str | None = None,
    expiring_within_days: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, le=500),
):
    q: dict = {}
    if customer_id:
        q["customer_id"] = customer_id
    if device_id:
        q["device_id"] = device_id
    if client_key:
        q["client_key"] = client_key
    if status:
        q["status"] = status
    if expiring_within_days is not None:
        cutoff = datetime.now(timezone.utc) + timedelta(days=expiring_within_days)
        q["status"] = "ACTIVE"
        q["end_date"] = {"$lte": cutoff}
    docs = svc.contracts_collection.find(q).sort("created_at", -1).limit(limit)
    return [_serialize(d) for d in docs]


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: str):
    return _serialize(_get_or_404(contract_id))


@router.post("/contracts/{contract_id}/cancel")
def cancel_contract(
    contract_id: str,
    reason: str = Body(..., embed=True),
    refund: bool = Body(default=False, embed=True),
):
    doc = _get_or_404(contract_id)
    if doc["status"] == "PENDING_ACTIVATION":
        svc.void(doc, reason)
    elif doc["status"] == "ACTIVE":
        svc.cancel(doc, reason, refunded=refund)
    else:
        raise HTTPException(status_code=409, detail=f"Cannot cancel a {doc['status']} contract")
    return _serialize(svc.contracts_collection.find_one({"_id": doc["_id"]}))


@router.post("/contracts/{contract_id}/resend")
def resend_documents(contract_id: str):
    doc = _get_or_404(contract_id)
    # TODO: enqueue email send. For now record the intent in the audit trail.
    svc.add_event(doc["_id"], "NOTIFIED", {"action": "resend_documents"})
    return {"status": "queued", "contract_id": contract_id}


@router.get("/customers/{customer_id}/contracts")
def customer_contracts(customer_id: str):
    """Powers the customer-hub 'Contracts' tab."""
    docs = svc.contracts_collection.find({"customer_id": customer_id}).sort("created_at", -1)
    return [_serialize(d) for d in docs]


# --- orders (parent) ---------------------------------------------------------
@router.get("/orders")
def list_orders(
    customer_id: str | None = None,
    client_key: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, le=500),
):
    q: dict = {}
    if customer_id:
        q["customer_id"] = customer_id
    if client_key:
        q["client_key"] = client_key
    if status:
        q["status"] = status
    docs = svc.orders_collection.find(q).sort("created_at", -1).limit(limit)
    return [_serialize(d) for d in docs]


@router.get("/orders/{order_id}")
def get_order(order_id: str):
    """Parent order (Stripe total + reconciliation) with its child contracts."""
    try:
        oid = ObjectId(order_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid order id")
    order = svc.orders_collection.find_one({"_id": oid})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    children = svc.contracts_collection.find({"order_id": oid}).sort("created_at", 1)
    result = _serialize(order)
    result["contracts"] = [_serialize(c) for c in children]
    return result


@router.get("/customers/{customer_id}/orders")
def customer_orders(customer_id: str):
    docs = svc.orders_collection.find({"customer_id": customer_id}).sort("created_at", -1)
    return [_serialize(d) for d in docs]


# --- scheduled jobs (point Railway Cron at these) ----------------------------
@router.post("/contracts/jobs/expiry-check")
def run_expiry_check():
    """Expire contracts past their end_date. Run daily via Railway Cron."""
    return svc.expiry_checker()


@router.post("/contracts/jobs/renewal-notify")
def run_renewal_notify(notice_days: int = 30):
    """Emit renewal notices for soon-to-expire contracts. Run daily via Railway Cron."""
    return svc.renewal_notifier(notice_days)


@router.get("/devices/{device_id}/contracts")
def device_contracts(device_id: str):
    docs = svc.contracts_collection.find({"device_id": device_id}).sort("created_at", -1)
    return [_serialize(d) for d in docs]
