"""Contract administration — core service layer (MongoDB).

This is the system of record for device-cover contracts. Contracts are issued from
the existing Stripe webhook (`/stripe/webhook`) on `checkout.session.completed` and
then administered over their lifetime (activate -> renew/expire/cancel).

Lives in its own `Contracts` collection (referenced by customer_id / device_id),
rather than as an array on the customer document, so it is queryable across customers.
"""
import os
import sys
import calendar
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient


def _add_months(dt: datetime, months: int) -> datetime:
    """Add whole months, clamping the day to the target month's length."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
contracts_collection = db["Contracts"]
orders_collection = db["ContractOrders"]
counters_collection = db["Counters"]
basket_collection = db["Basket_Quotes"]

# --- status model -----------------------------------------------------------
STATUSES = ("PENDING_ACTIVATION", "ACTIVE", "EXPIRED", "CANCELLED", "RENEWED", "VOID")
_TRANSITIONS = {
    "PENDING_ACTIVATION": {"ACTIVE", "VOID"},
    "ACTIVE": {"EXPIRED", "CANCELLED", "RENEWED"},
    "EXPIRED": set(),
    "CANCELLED": set(),
    "RENEWED": set(),
    "VOID": set(),
}


class InvalidTransition(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_sequence(kind: str, prefix: str) -> str:
    """Atomic, monotonic reference: <PREFIX>-<year>-000123."""
    year = datetime.now(timezone.utc).year
    key = f"{kind}-{year}"
    doc = counters_collection.find_one_and_update(
        {"_id": key},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    seq = doc["seq"] if doc and "seq" in doc else 1
    return f"{prefix}-{year}-{seq:06d}"


def _next_reference() -> str:
    """Per-device contract reference: ACT-<year>-000123."""
    return _next_sequence("contract", "ACT")


def _next_order_reference() -> str:
    """Parent order reference: ORD-<year>-000123."""
    return _next_sequence("order", "ORD")


def _event(type_: str, payload: dict | None = None, actor: str = "system") -> dict:
    return {"type": type_, "payload": payload or {}, "actor": actor, "at": _now()}


# --- issuance ----------------------------------------------------------------
def _item_term_months(item: dict) -> int:
    """Derive cover term from a basket item. Extend this mapping as products grow."""
    for key in ("term_months", "term"):
        if item.get(key):
            try:
                return int(item[key])
            except (TypeError, ValueError):
                pass
    if item.get("years"):
        try:
            return int(item["years"]) * 12
        except (TypeError, ValueError):
            pass
    return 12  # default cover term


def _item_dedupe_key(session_id: str, item: dict, index: int) -> str:
    """Stable per-item key so retries never double-issue a contract."""
    ident = item.get("quote_id") or f"{item.get('deviceId','')}:{item.get('product_id','')}:{index}"
    return f"{session_id}::{ident}"


def _get_or_create_order(session: dict, customer_id: str, basket_id: str | None,
                         billing: str) -> dict:
    """Parent order — one per checkout session — holding the verified Stripe total.

    Idempotent on stripe_session_id.
    """
    session_id = session.get("id")
    if session_id:
        existing = orders_collection.find_one({"stripe_session_id": session_id})
        if existing:
            return existing

    amount_total = session.get("amount_total")
    order = {
        "order_reference": _next_order_reference(),
        "basket_id": basket_id or "",
        "customer_id": customer_id,
        "client_key": "",  # filled from first child below
        "status": "PENDING_ACTIVATION",
        "billing": billing,
        "stripe_session_id": session_id,
        "stripe_customer_id": session.get("customer"),
        "stripe_payment_intent_id": session.get("payment_intent"),
        "stripe_subscription_id": session.get("subscription"),
        "stripe_amount_total": (amount_total / 100) if amount_total is not None else None,
        "currency": (session.get("currency") or "gbp").upper(),
        "item_count": 0,
        "items_price_sum": 0.0,
        "amount_reconciled": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        res = orders_collection.insert_one(order)
        order["_id"] = res.inserted_id
    except Exception:
        # Lost a concurrent race on the unique session index — reload the winner.
        order = orders_collection.find_one({"stripe_session_id": session_id})
    return order


def _refresh_order(order_id) -> None:
    """Recompute the parent order's rolled-up status and amount reconciliation."""
    children = list(contracts_collection.find({"order_id": order_id}))
    statuses = {c["status"] for c in children}
    if not statuses:
        rollup = "PENDING_ACTIVATION"
    elif statuses <= {"CANCELLED", "VOID"}:
        rollup = "CANCELLED"
    elif statuses <= {"EXPIRED", "CANCELLED", "VOID"}:
        rollup = "EXPIRED"
    elif "ACTIVE" in statuses or "RENEWED" in statuses:
        rollup = "ACTIVE"
    else:
        rollup = "PENDING_ACTIVATION"

    items_sum = round(sum((c.get("price") or 0) for c in children), 2)
    order = orders_collection.find_one({"_id": order_id})
    total = order.get("stripe_amount_total") if order else None
    reconciled = None
    if total is not None:
        reconciled = abs(items_sum - total) < 0.01

    update = {
        "status": rollup,
        "item_count": len(children),
        "items_price_sum": items_sum,
        "amount_reconciled": reconciled,
        "updated_at": _now(),
    }
    if order and not order.get("client_key") and children:
        update["client_key"] = children[0].get("client_key", "")
    orders_collection.update_one({"_id": order_id}, {"$set": update})


def create_contract(session: dict, customer_id: str) -> list[dict]:
    """Issue a parent order + one child contract per basket item for a completed
    Stripe checkout session.

    The basket is the source of truth (loaded by `basket_id` from the session
    metadata). The parent ContractOrders doc stores the verified Stripe
    `amount_total`; each child Contracts doc stores its own item price and
    device. Idempotent: parent on session id, children on `dedupe_key`.
    Returns the list of child contracts.
    """
    session_id = session.get("id")
    meta = session.get("metadata") or {}
    basket_id = meta.get("basket_id")

    # Billing model comes from the Stripe session mode / subscription, not metadata.
    is_subscription = bool(session.get("subscription")) or session.get("mode") == "subscription"
    billing = "MONTHLY" if is_subscription else "ONE_OFF"
    paid = session.get("payment_status") == "paid"

    # Load basket items (source of truth). Fall back to a single metadata-based
    # contract if the basket can't be loaded, so issuance never silently no-ops.
    items: list[dict] = []
    basket = None
    if basket_id:
        try:
            basket = basket_collection.find_one({"_id": ObjectId(basket_id)})
        except (InvalidId, TypeError):
            basket = None
    if basket:
        items = basket.get("Basket", []) or []
    if not items:
        items = [{}]  # degenerate: one contract from session-level data only

    order = _get_or_create_order(session, customer_id, basket_id, billing)
    order_id = order["_id"]

    out: list[dict] = []
    for index, item in enumerate(items):
        dedupe_key = _item_dedupe_key(session_id or "", item, index)

        existing = contracts_collection.find_one({"dedupe_key": dedupe_key})
        if existing:
            print(f"[Contract] {dedupe_key} already issued as {existing.get('reference')}",
                  file=sys.stderr)
            out.append(existing)
            continue

        term_months = _item_term_months(item)
        price = item.get("rounded_price")
        if price is None and item.get("rounded_price_pence") is not None:
            price = item["rounded_price_pence"] / 100
        currency = (item.get("currency") or session.get("currency") or "gbp").upper()

        doc = {
            "reference": _next_reference(),
            "order_id": order_id,
            "order_reference": order["order_reference"],
            "customer_id": customer_id,
            "device_id": item.get("deviceId", ""),
            "offer_id": item.get("product_id", ""),
            "quote_id": item.get("quote_id", ""),
            "basket_id": basket_id or "",
            "client_key": item.get("client") or meta.get("client") or "",
            "source": item.get("source") or meta.get("source") or "",
            "status": "PENDING_ACTIVATION",
            "cover_type": item.get("category"),
            "term_months": term_months,
            "start_date": None,
            "end_date": None,
            "billing": billing,
            "price": price,
            "currency": currency,
            "auto_renew": billing == "MONTHLY",
            "stripe_customer_id": session.get("customer"),
            "stripe_session_id": session_id,
            "stripe_payment_intent_id": session.get("payment_intent"),
            "stripe_subscription_id": session.get("subscription"),
            "dedupe_key": dedupe_key,
            "document_url": None,
            "ipid_url": None,
            "events": [_event("ISSUED", {"checkout_session": session_id, "basket_item": index})],
            "payments": [],
            "created_at": _now(),
            "updated_at": _now(),
            "cancelled_at": None,
            "cancel_reason": None,
        }

        result = contracts_collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        print(f"[Contract] Issued {doc['reference']} (device {doc['device_id']}, "
              f"offer {doc['offer_id']}) under order {order['order_reference']}",
              file=sys.stderr)

        # One-off checkouts arrive already paid -> activate immediately.
        if billing == "ONE_OFF" and paid:
            _record_payment(doc, session_id, "INITIAL", amount=price, currency=currency)
            activate(doc, term_months)

        out.append(contracts_collection.find_one({"_id": result.inserted_id}))

    _refresh_order(order_id)
    return out


# --- lifecycle ---------------------------------------------------------------
def _apply(doc: dict, new_status: str, event: dict, extra: dict | None = None) -> None:
    if new_status not in _TRANSITIONS.get(doc["status"], set()):
        raise InvalidTransition(f"{doc['status']} -> {new_status} not allowed")
    update = {"status": new_status, "updated_at": _now()}
    if extra:
        update.update(extra)
    contracts_collection.update_one(
        {"_id": doc["_id"]},
        {"$set": update, "$push": {"events": event}},
    )
    doc.update(update)
    # Keep the parent order's rolled-up status in sync.
    if doc.get("order_id"):
        _refresh_order(doc["order_id"])


def activate(doc: dict, term_months: int) -> None:
    start = _now()
    end = _add_months(start, term_months)
    _apply(doc, "ACTIVE", _event("ACTIVATED", {"term_months": term_months}),
           extra={"start_date": start, "end_date": end, "term_months": term_months})


def expire(doc: dict) -> None:
    _apply(doc, "EXPIRED", _event("EXPIRED"))


def cancel(doc: dict, reason: str, refunded: bool = False) -> None:
    _apply(doc, "CANCELLED",
           _event("REFUNDED" if refunded else "CANCELLED", {"reason": reason}),
           extra={"cancelled_at": _now(), "cancel_reason": reason})


def void(doc: dict, reason: str) -> None:
    _apply(doc, "VOID", _event("CANCELLED", {"reason": reason, "void": True}),
           extra={"cancelled_at": _now(), "cancel_reason": reason})


def renew(doc: dict) -> dict:
    """ACTIVE -> RENEWED, spawning a fresh ACTIVE contract for the next term."""
    _apply(doc, "RENEWED", _event("RENEWED"))
    term = doc.get("term_months") or 12
    new_doc = {
        "reference": _next_reference(),
        "order_id": doc.get("order_id"),
        "order_reference": doc.get("order_reference"),
        "customer_id": doc["customer_id"],
        "device_id": doc.get("device_id", ""),
        "offer_id": doc.get("offer_id", ""),
        "quote_id": doc.get("quote_id", ""),
        "basket_id": doc.get("basket_id", ""),
        "client_key": doc.get("client_key", ""),
        "source": doc.get("source", ""),
        "status": "PENDING_ACTIVATION",
        "cover_type": doc.get("cover_type"),
        "term_months": term,
        "start_date": None,
        "end_date": None,
        "billing": doc.get("billing", "MONTHLY"),
        "price": doc.get("price"),
        "currency": doc.get("currency"),
        "auto_renew": doc.get("auto_renew", True),
        "stripe_customer_id": doc.get("stripe_customer_id"),
        "stripe_session_id": None,
        "stripe_payment_intent_id": None,
        "stripe_subscription_id": doc.get("stripe_subscription_id"),
        # No dedupe_key: a renewal is not tied to a checkout session item. Omitting
        # the field (rather than setting null) keeps it out of the unique index.
        "document_url": None,
        "ipid_url": None,
        "events": [_event("ISSUED", {"renewed_from": str(doc["_id"])})],
        "payments": [],
        "created_at": _now(),
        "updated_at": _now(),
        "cancelled_at": None,
        "cancel_reason": None,
    }
    res = contracts_collection.insert_one(new_doc)
    new_doc["_id"] = res.inserted_id
    activate(new_doc, term)
    return contracts_collection.find_one({"_id": res.inserted_id})


def add_event(doc_id, type_: str, payload: dict | None = None, actor: str = "system") -> None:
    contracts_collection.update_one(
        {"_id": doc_id}, {"$push": {"events": _event(type_, payload, actor)}}
    )


# --- scheduled jobs ----------------------------------------------------------
def expiry_checker() -> dict:
    """ACTIVE -> EXPIRED for any contract whose end_date has passed.

    Intended to be run daily (e.g. via Railway Cron hitting the job endpoint).
    """
    now = _now()
    expired = 0
    touched_orders = set()
    for doc in contracts_collection.find({"status": "ACTIVE", "end_date": {"$lt": now}}):
        try:
            expire(doc)
            expired += 1
            if doc.get("order_id"):
                touched_orders.add(doc["order_id"])
        except InvalidTransition:
            continue
    print(f"[Contract] expiry_checker expired {expired} contracts", file=sys.stderr)
    return {"expired": expired, "orders_touched": len(touched_orders)}


def renewal_notifier(notice_days: int = 30) -> dict:
    """Emit a NOTIFIED event for contracts within `notice_days` of end_date.

    Email send is stubbed — this records the notice in the audit trail so a real
    sender can be layered on without changing the cron contract.
    """
    from datetime import timedelta

    now = _now()
    cutoff = now + timedelta(days=notice_days)
    notified = 0
    for doc in contracts_collection.find(
        {"status": "ACTIVE", "end_date": {"$gte": now, "$lte": cutoff}}
    ):
        add_event(doc["_id"], "NOTIFIED", {"reason": "renewal_notice", "days": notice_days})
        notified += 1
    print(f"[Contract] renewal_notifier notified {notified} contracts", file=sys.stderr)
    return {"notified": notified, "notice_days": notice_days}


def handle_invoice_paid(invoice: dict, stripe_event_id: str | None = None) -> list[dict]:
    """Activate (or renew) every contract on a subscription for one paid invoice.

    Driven by Stripe `invoice.paid`. A single subscription can back many device
    contracts (one monthly basket -> many policies), so this applies the event to
    ALL of them, not just one. Each child records its own item price as the
    payment amount (the invoice amount is the subscription total across devices).
    Returns the list of affected contracts.
    """
    sub_id = invoice.get("subscription")
    if not sub_id:
        return []
    reason = invoice.get("billing_reason")
    currency = (invoice.get("currency") or "gbp").upper()
    invoice_id = invoice.get("id")

    affected: list[dict] = []

    if reason == "subscription_create":
        # Activate all pending contracts for this subscription.
        pending = list(contracts_collection.find(
            {"stripe_subscription_id": sub_id, "status": "PENDING_ACTIVATION"}
        ))
        for doc in pending:
            _record_payment(doc, invoice_id, "INITIAL", doc.get("price"),
                            doc.get("currency") or currency, stripe_event_id=stripe_event_id)
            activate(doc, doc.get("term_months") or 12)
            affected.append(contracts_collection.find_one({"_id": doc["_id"]}))

    elif reason == "subscription_cycle":
        # Renew all currently-active contracts. Snapshot first: renew() creates new
        # ACTIVE docs, which must not be picked up again in the same pass.
        active = list(contracts_collection.find(
            {"stripe_subscription_id": sub_id, "status": "ACTIVE"}
        ))
        for doc in active:
            _record_payment(doc, invoice_id, "RENEWAL", doc.get("price"),
                            doc.get("currency") or currency, stripe_event_id=stripe_event_id)
            affected.append(renew(doc))

    return affected


def handle_charge_refunded(charge: dict, stripe_event_id: str | None = None) -> dict | None:
    """On `charge.refunded`: void a pending contract, or cancel an active one."""
    pi = charge.get("payment_intent")
    if not pi:
        return None
    doc = contracts_collection.find_one({"stripe_payment_intent_id": pi})
    if not doc:
        return None
    amount = charge.get("amount_refunded")
    amount = (amount / 100) if amount is not None else None
    _record_payment(doc, charge.get("id"), "INITIAL",
                    amount=amount, currency=(charge.get("currency") or "gbp").upper(),
                    status="REFUNDED", stripe_event_id=stripe_event_id)
    if doc["status"] == "PENDING_ACTIVATION":
        void(doc, "refunded before activation")
    elif doc["status"] == "ACTIVE":
        cancel(doc, "refunded", refunded=True)
    return contracts_collection.find_one({"_id": doc["_id"]})


def handle_subscription_deleted(subscription: dict) -> dict | None:
    """On `customer.subscription.deleted`: cancel the active contract."""
    sub_id = subscription.get("id")
    doc = contracts_collection.find_one(
        {"stripe_subscription_id": sub_id, "status": "ACTIVE"}
    )
    if not doc:
        return None
    cancel(doc, "subscription cancelled")
    return contracts_collection.find_one({"_id": doc["_id"]})


def _record_payment(doc: dict, object_id, kind: str, amount=None, currency=None,
                    status: str = "SUCCEEDED", stripe_event_id: str | None = None) -> None:
    payment = {
        "stripe_event_id": stripe_event_id,
        "stripe_object_id": object_id,
        "kind": kind,
        "amount": amount,
        "currency": currency,
        "status": status,
        "at": _now(),
    }
    contracts_collection.update_one({"_id": doc["_id"]}, {"$push": {"payments": payment}})
