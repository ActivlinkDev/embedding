"""Contract administration endpoints (read + manage).

Issuance happens via the Stripe webhook; these endpoints power the customer-hub
'Contracts' tab and internal administration. All routes require the bearer token.
"""
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from utils.api_docs import error, json_response, secured
from utils.dependencies import caller_client_key, verify_token

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


def _scoped(q: dict, scope: str | None) -> dict:
    """Constrain a query to the caller's tenant. A pinned caller never sees records whose
    client_key is missing or empty, so unattributed rows stay hidden rather than leaking."""
    if scope:
        q["client_key"] = scope
    return q


def _get_or_404(contract_id: str, scope: str | None) -> dict:
    try:
        oid = ObjectId(contract_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid contract id")
    doc = svc.contracts_collection.find_one(_scoped({"_id": oid}, scope))
    if not doc:
        # 404 rather than 403: a pinned caller must not learn that another tenant's id exists.
        raise HTTPException(status_code=404, detail="Contract not found")
    return doc


@router.get(
    "/contracts",
    summary="Search contracts",
    response_description="Matching contracts, newest first.",
    responses=secured({
        200: json_response("Matching contracts. An empty array is a normal result.", [{
                "id": "68d0e1f2a3b4c5d6e7f80011",
                "order_id": "68d0e1f2a3b4c5d6e7f80001",
                "client_key": "acme_uk_live",
                "customer_id": "6820f1c9a4b21d0f8c9e9001",
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "product_id": "ACME-EW-STD",
                "status": "ACTIVE",
                "term_months": 24,
                "start_date": "2026-08-06T10:20:00Z",
                "end_date": "2028-08-06T10:20:00Z",
                "premium_pence": 7149,
                "currency": "GBP",
                "created_at": "2026-08-06T10:20:00Z",
            }]),
    }),
)
def list_contracts(
    customer_id: str | None = Query(None, description="Only contracts belonging to this customer.", examples=["6820f1c9a4b21d0f8c9e9001"]),
    device_id: str | None = Query(None, description="Only contracts covering this device.", examples=["6820f1c9a4b21d0f8c9e4471"]),
    client_key: str | None = Query(
        None,
        description="Only contracts for this tenant. A caller pinned to a ClientKey is already limited to their own.",
        examples=["acme_uk_live"],
    ),
    status: str | None = Query(
        None,
        description="Only contracts in this state, e.g. `ACTIVE`, `PENDING_ACTIVATION`, `EXPIRED`, `CANCELLED`, `VOID`. **Overridden when `expiring_within_days` is used.**",
        examples=["ACTIVE"],
    ),
    expiring_within_days: int | None = Query(
        default=None,
        ge=0,
        description="Only `ACTIVE` contracts ending within this many days. **Forces `status` to `ACTIVE`**, ignoring any `status` sent alongside it.",
        examples=[30],
    ),
    limit: int = Query(default=100, le=500, description="Maximum contracts to return. Capped at 500.", examples=[100]),
    scope: str | None = Depends(caller_client_key),
):
    """
    Search contracts, newest first. Every filter is optional; with none supplied you get the most
    recent contracts the caller may see.

    **Tenant-scoped.** A caller pinned to a ClientKey sees only that tenant's contracts, and
    never records with a missing or empty `client_key` — unattributed rows stay hidden rather
    than leaking. Callers holding `clientkey:*` see everything.

    `expiring_within_days` is a renewal shortcut: it restricts to `ACTIVE` contracts ending
    inside the window and **overrides** any `status` filter sent with it.

    There is no pagination — raise `limit` (up to 500) or narrow the filters.
    """
    q: dict = _scoped({}, scope)
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


@router.get(
    "/contracts/{contract_id}",
    summary="Fetch a single contract",
    response_description="The contract document.",
    responses=secured({
        200: json_response("The contract was found and is visible to this caller.", {
                "id": "68d0e1f2a3b4c5d6e7f80011",
                "order_id": "68d0e1f2a3b4c5d6e7f80001",
                "client_key": "acme_uk_live",
                "customer_id": "6820f1c9a4b21d0f8c9e9001",
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "product_id": "ACME-EW-STD",
                "status": "ACTIVE",
                "term_months": 24,
                "start_date": "2026-08-06T10:20:00Z",
                "end_date": "2028-08-06T10:20:00Z",
                "premium_pence": 7149,
                "currency": "GBP",
                "created_at": "2026-08-06T10:20:00Z",
            }),
        400: error("`contract_id` is not a valid 24-character ObjectId.", "Invalid contract id"),
        404: error("No such contract — **or** it belongs to another tenant.", "Contract not found"),
    }),
)
def get_contract(contract_id: str, scope: str | None = Depends(caller_client_key)):
    """
    Fetch one contract by id.

    Path parameter `contract_id` is mandatory. A contract belonging to another tenant returns
    `404`, not `403`, so a pinned caller cannot discover that another tenant's id exists.
    """
    return _serialize(_get_or_404(contract_id, scope))


@router.post(
    "/contracts/{contract_id}/cancel",
    summary="Cancel or void a contract",
    response_description="The contract in its new state.",
    responses=secured({
        200: json_response(
            "The contract was cancelled (or voided, if it had not started).",
            {**{
                "id": "68d0e1f2a3b4c5d6e7f80011",
                "order_id": "68d0e1f2a3b4c5d6e7f80001",
                "client_key": "acme_uk_live",
                "customer_id": "6820f1c9a4b21d0f8c9e9001",
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "product_id": "ACME-EW-STD",
                "status": "ACTIVE",
                "term_months": 24,
                "start_date": "2026-08-06T10:20:00Z",
                "end_date": "2028-08-06T10:20:00Z",
                "premium_pence": 7149,
                "currency": "GBP",
                "created_at": "2026-08-06T10:20:00Z",
            }, "status": "CANCELLED"},
        ),
        400: error("`contract_id` is not a valid 24-character ObjectId.", "Invalid contract id"),
        404: error("No such contract, or it belongs to another tenant.", "Contract not found"),
        409: error(
            "The contract is in a state that cannot be cancelled — only `ACTIVE` and "
            "`PENDING_ACTIVATION` can.",
            "Cannot cancel a EXPIRED contract",
        ),
    }),
)
def cancel_contract(
    contract_id: str,
    reason: str = Body(..., embed=True, description="**Mandatory.** Why the contract is being cancelled. Recorded in the audit trail."),
    refund: bool = Body(default=False, embed=True, description="Mark the cancellation as refunded. Only meaningful for an `ACTIVE` contract."),
    scope: str | None = Depends(caller_client_key),
):
    """
    End a contract early.

    **What happens depends on the current status:**

    - `PENDING_ACTIVATION` → **voided**: cover never started, so it is treated as though it never
      existed. `refund` is not applicable.
    - `ACTIVE` → **cancelled**, with `refund` recorded against it.
    - Anything else (`EXPIRED`, already `CANCELLED`, `VOID`) → `409`; there is nothing to end.

    `reason` is mandatory and goes into the contract's audit trail, so keep it meaningful.
    Note that `refund: true` **records** a refund — it does not issue one through Stripe.

    This is not reversible: a cancelled contract cannot be reactivated.
    """
    doc = _get_or_404(contract_id, scope)
    if doc["status"] == "PENDING_ACTIVATION":
        svc.void(doc, reason)
    elif doc["status"] == "ACTIVE":
        svc.cancel(doc, reason, refunded=refund)
    else:
        raise HTTPException(status_code=409, detail=f"Cannot cancel a {doc['status']} contract")
    return _serialize(svc.contracts_collection.find_one({"_id": doc["_id"]}))


@router.post(
    "/contracts/{contract_id}/resend",
    summary="Queue a re-send of a contract's documents",
    response_description="Confirmation that the re-send was recorded.",
    responses=secured({
        200: json_response("The request was recorded.", {"status": "queued", "contract_id": "68d0e1f2a3b4c5d6e7f80011"}),
        400: error("`contract_id` is not a valid 24-character ObjectId.", "Invalid contract id"),
        404: error("No such contract, or it belongs to another tenant.", "Contract not found"),
    }),
)
def resend_documents(contract_id: str, scope: str | None = Depends(caller_client_key)):
    """
    Ask for a contract's documents to be sent to the customer again.

    **Currently records intent only.** A `NOTIFIED` event is written to the contract's audit
    trail; the email delivery step is not yet implemented, so `{"status": "queued"}` means the
    request was logged, **not** that anything has been sent. Treat it as an audit entry until
    delivery is wired up.

    Path parameter `contract_id` is mandatory.
    """
    doc = _get_or_404(contract_id, scope)
    # TODO: enqueue email send. For now record the intent in the audit trail.
    svc.add_event(doc["_id"], "NOTIFIED", {"action": "resend_documents"})
    return {"status": "queued", "contract_id": contract_id}


@router.get(
    "/customers/{customer_id}/contracts",
    summary="List a customer's contracts",
    response_description="Every contract belonging to the customer, newest first.",
    responses=secured({
        200: json_response("The customer's contracts. An unknown customer simply returns `[]`.", [{
                "id": "68d0e1f2a3b4c5d6e7f80011",
                "order_id": "68d0e1f2a3b4c5d6e7f80001",
                "client_key": "acme_uk_live",
                "customer_id": "6820f1c9a4b21d0f8c9e9001",
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "product_id": "ACME-EW-STD",
                "status": "ACTIVE",
                "term_months": 24,
                "start_date": "2026-08-06T10:20:00Z",
                "end_date": "2028-08-06T10:20:00Z",
                "premium_pence": 7149,
                "currency": "GBP",
                "created_at": "2026-08-06T10:20:00Z",
            }]),
    }),
)
def customer_contracts(customer_id: str, scope: str | None = Depends(caller_client_key)):
    """
    Every contract belonging to one customer, newest first — this powers the customer hub's
    *Contracts* tab.

    Path parameter `customer_id` is mandatory. Results are limited to the caller's tenant, and
    there is no limit or pagination: a customer's full history is returned. An unknown customer
    id is not an error — it returns an empty array.
    """
    docs = svc.contracts_collection.find(
        _scoped({"customer_id": customer_id}, scope)
    ).sort("created_at", -1)
    return [_serialize(d) for d in docs]


# --- orders (parent) ---------------------------------------------------------
@router.get(
    "/orders",
    summary="Search orders",
    response_description="Matching orders, newest first.",
    responses=secured({
        200: json_response(
            "Matching orders. Contracts are **not** included here — fetch a single order for those.",
            [
                {
                    "id": "68d0e1f2a3b4c5d6e7f80001",
                    "client_key": "acme_uk_live",
                    "customer_id": "6820f1c9a4b21d0f8c9e9001",
                    "status": "ACTIVE",
                    "total_pence": 14298,
                    "currency": "GBP",
                    "stripe_session_id": "cs_test_a1B2c3D4e5F6",
                    "created_at": "2026-08-06T10:20:00Z",
                }
            ],
        ),
    }),
)
def list_orders(
    customer_id: str | None = Query(None, description="Only orders placed by this customer.", examples=["6820f1c9a4b21d0f8c9e9001"]),
    client_key: str | None = Query(None, description="Only orders for this tenant.", examples=["acme_uk_live"]),
    status: str | None = Query(None, description="Only orders in this state, e.g. `ACTIVE`, `CANCELLED`.", examples=["ACTIVE"]),
    limit: int = Query(default=100, le=500, description="Maximum orders to return. Capped at 500.", examples=[100]),
    scope: str | None = Depends(caller_client_key),
):
    """
    Search orders, newest first.

    An **order** is the parent of one payment: it holds the Stripe total and reconciliation
    details, and owns one contract per device covered. Use this to answer "what did the customer
    pay?", and `GET /contracts` to answer "what cover is in force?".

    Every filter is optional, results are limited to the caller's tenant, and there is no
    pagination — raise `limit` (up to 500) or narrow the filters. Child contracts are not
    included; fetch a single order with `GET /orders/{order_id}` for those.
    """
    q: dict = _scoped({}, scope)
    if customer_id:
        q["customer_id"] = customer_id
    if client_key:
        q["client_key"] = client_key
    if status:
        q["status"] = status
    docs = svc.orders_collection.find(q).sort("created_at", -1).limit(limit)
    return [_serialize(d) for d in docs]


@router.get(
    "/orders/{order_id}",
    summary="Fetch an order with its contracts",
    response_description="The order document plus every contract it produced.",
    responses=secured({
        200: json_response(
            "The order was found, with its child contracts attached.",
            {
                "id": "68d0e1f2a3b4c5d6e7f80001",
                "client_key": "acme_uk_live",
                "customer_id": "6820f1c9a4b21d0f8c9e9001",
                "status": "ACTIVE",
                "total_pence": 14298,
                "currency": "GBP",
                "stripe_session_id": "cs_test_a1B2c3D4e5F6",
                "created_at": "2026-08-06T10:20:00Z",
                "contracts": [{
                "id": "68d0e1f2a3b4c5d6e7f80011",
                "order_id": "68d0e1f2a3b4c5d6e7f80001",
                "client_key": "acme_uk_live",
                "customer_id": "6820f1c9a4b21d0f8c9e9001",
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "product_id": "ACME-EW-STD",
                "status": "ACTIVE",
                "term_months": 24,
                "start_date": "2026-08-06T10:20:00Z",
                "end_date": "2028-08-06T10:20:00Z",
                "premium_pence": 7149,
                "currency": "GBP",
                "created_at": "2026-08-06T10:20:00Z",
            }],
            },
        ),
        400: error("`order_id` is not a valid 24-character ObjectId.", "Invalid order id"),
        404: error("No such order, or it belongs to another tenant.", "Order not found"),
    }),
)
def get_order(order_id: str, scope: str | None = Depends(caller_client_key)):
    """
    Fetch one order together with every contract it produced.

    The order carries the Stripe total and reconciliation details; `contracts` holds one entry per
    device covered, oldest first. A basket paid in a single Stripe session therefore appears as
    one order with several contracts.

    Path parameter `order_id` is mandatory. An order belonging to another tenant returns `404`.
    """
    try:
        oid = ObjectId(order_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid order id")
    order = svc.orders_collection.find_one(_scoped({"_id": oid}, scope))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    children = svc.contracts_collection.find(
        _scoped({"order_id": oid}, scope)
    ).sort("created_at", 1)
    result = _serialize(order)
    result["contracts"] = [_serialize(c) for c in children]
    return result


@router.get(
    "/customers/{customer_id}/orders",
    summary="List a customer's orders",
    response_description="Every order placed by the customer, newest first.",
    responses=secured({
        200: json_response(
            "The customer's orders. An unknown customer simply returns `[]`.",
            [
                {
                    "id": "68d0e1f2a3b4c5d6e7f80001",
                    "client_key": "acme_uk_live",
                    "customer_id": "6820f1c9a4b21d0f8c9e9001",
                    "status": "ACTIVE",
                    "total_pence": 14298,
                    "currency": "GBP",
                    "created_at": "2026-08-06T10:20:00Z",
                }
            ],
        ),
    }),
)
def customer_orders(customer_id: str, scope: str | None = Depends(caller_client_key)):
    """
    Every order placed by one customer, newest first — their purchase history.

    Path parameter `customer_id` is mandatory. Results are limited to the caller's tenant and are
    not paginated. Child contracts are not included; fetch an order with
    `GET /orders/{order_id}` for those.
    """
    docs = svc.orders_collection.find(
        _scoped({"customer_id": customer_id}, scope)
    ).sort("created_at", -1)
    return [_serialize(d) for d in docs]


# --- scheduled jobs (point Railway Cron at these) ----------------------------
def _deny_if_pinned(scope: str | None) -> None:
    """These jobs sweep every tenant's contracts, so they are not available to a pinned caller."""
    if scope:
        raise HTTPException(
            status_code=403,
            detail="Maintenance jobs are not available to tenant-scoped callers",
        )


@router.post(
    "/contracts/jobs/expiry-check",
    summary="Maintenance job: expire contracts past their end date",
    response_description="How many contracts were expired and how many orders were affected.",
    responses=secured({
        200: json_response("The sweep completed.", {"expired": 4, "orders_touched": 3}),
        403: error(
            "The caller is pinned to a ClientKey. This job sweeps every tenant, so it needs an "
            "unpinned credential.",
            "Maintenance jobs are not available to tenant-scoped callers",
        ),
    }),
)
def run_expiry_check(scope: str | None = Depends(caller_client_key)):
    """
    **Scheduled maintenance job.** Move every `ACTIVE` contract past its `end_date` to `EXPIRED`,
    and update the status of the orders that own them.

    Takes no parameters. Intended to run **daily** from a scheduler (Railway Cron), not from an
    application — contracts do not expire on their own, so if this stops running they stay
    `ACTIVE` past their end date.

    It sweeps **all tenants at once**, so a caller pinned to a ClientKey is refused with `403`.
    Safe to run repeatedly: contracts already expired are skipped, and a second run in the same
    day reports `{"expired": 0}`.
    """
    _deny_if_pinned(scope)
    return svc.expiry_checker()


@router.post(
    "/contracts/jobs/renewal-notify",
    summary="Maintenance job: flag contracts due for renewal",
    response_description="How many contracts were notified, and the window used.",
    responses=secured({
        200: json_response("The sweep completed.", {"notified": 12, "notice_days": 30}),
        403: error(
            "The caller is pinned to a ClientKey. This job sweeps every tenant, so it needs an "
            "unpinned credential.",
            "Maintenance jobs are not available to tenant-scoped callers",
        ),
    }),
)
def run_renewal_notify(
    notice_days: int = Query(
        30,
        description="How many days ahead of `end_date` to notify. Defaults to 30.",
        examples=[30],
    ),
    scope: str | None = Depends(caller_client_key),
):
    """
    **Scheduled maintenance job.** Record a renewal notice against every contract ending within
    `notice_days`.

    **Email delivery is stubbed.** A `NOTIFIED` event is written to each contract's audit trail
    so a real sender can pick them up later; `notified` counts audit entries written, not
    messages sent.

    Intended to run **daily** from a scheduler (Railway Cron). It sweeps all tenants, so a caller
    pinned to a ClientKey is refused with `403`.
    """
    _deny_if_pinned(scope)
    return svc.renewal_notifier(notice_days)


@router.get(
    "/devices/{device_id}/contracts",
    summary="List a device's contracts",
    response_description="Every contract covering the device, newest first.",
    responses=secured({
        200: json_response("The device's contracts. An unknown device simply returns `[]`.", [{
                "id": "68d0e1f2a3b4c5d6e7f80011",
                "order_id": "68d0e1f2a3b4c5d6e7f80001",
                "client_key": "acme_uk_live",
                "customer_id": "6820f1c9a4b21d0f8c9e9001",
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "product_id": "ACME-EW-STD",
                "status": "ACTIVE",
                "term_months": 24,
                "start_date": "2026-08-06T10:20:00Z",
                "end_date": "2028-08-06T10:20:00Z",
                "premium_pence": 7149,
                "currency": "GBP",
                "created_at": "2026-08-06T10:20:00Z",
            }]),
    }),
)
def device_contracts(device_id: str, scope: str | None = Depends(caller_client_key)):
    """
    Every contract covering one device, newest first — including expired and cancelled ones, so
    the full cover history is visible.

    Path parameter `device_id` is mandatory. Results are limited to the caller's tenant and are
    not paginated. To find only current cover, filter the result on `status: "ACTIVE"`, or use
    `GET /contracts?device_id=…&status=ACTIVE`.
    """
    docs = svc.contracts_collection.find(
        _scoped({"device_id": device_id}, scope)
    ).sort("created_at", -1)
    return [_serialize(d) for d in docs]
