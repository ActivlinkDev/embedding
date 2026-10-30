"""Registration and plan confirmation emails, sent through Brevo.

Every send follows the same steps:

1. Resolve the tenant from the records themselves (``Devices.client`` + ``source``), never from
   anything the browser sent.
2. Skip, and record why, when the client has not enabled this email or there is no deliverable
   address. The placeholder address the customer step invents counts as no address.
3. **Claim** ``EmailLog.dedupe_key`` before calling Brevo. A replayed Stripe webhook or a
   repeated pairing call finds the claim and sends nothing. Only a claim whose send *failed*
   can be re-claimed.
4. Create one Cloud ID link per item and send. Brevo is called once: a timeout is recorded as
   failed rather than retried, because Brevo may already have accepted it.

These functions never raise: they run after the customer's registration or payment has
succeeded, and an email problem must not undo or retry that.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from datetime import datetime, timedelta
from typing import Iterable, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from routers.comms import cloud_links, store
from routers.comms.brevo_client import BrevoEmailClient
from routers.comms.email_config import (
    EmailContext, build_params, client_key_for, render_fallback_html, resolve_email_context,
)

TEST_SENDS_PER_HOUR = 10

# Strong references to in-flight sends started from request handlers, so the event loop's
# weak reference is not the only thing keeping them alive.
_pending: set[asyncio.Task] = set()


def schedule(coro) -> None:
    """Run a send in the background of the current event loop."""
    task = asyncio.get_running_loop().create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def _log(message: str) -> None:
    print(f"[EMAIL] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Claim / record
# ---------------------------------------------------------------------------

def _claim(dedupe_key: str, kind: str, client_key: Optional[str], ref_id: str) -> bool:
    row = {"dedupe_key": dedupe_key, "kind": kind, "client_key": client_key, "ref_id": ref_id,
           "status": "pending", "created_at": store.now(), "updated_at": store.now()}
    try:
        store.email_log.insert_one(row)
        return True
    except DuplicateKeyError:
        retried = store.email_log.update_one(
            {"dedupe_key": dedupe_key, "status": "failed"},
            {"$set": {"status": "pending", "updated_at": store.now()}, "$inc": {"retries": 1}})
        return bool(retried.modified_count)


def _record(dedupe_key: str, status: str, **fields) -> None:
    store.email_log.update_one({"dedupe_key": dedupe_key},
                               {"$set": {"status": status, "updated_at": store.now(), **fields}})


def _skip(dedupe_key: str, kind: str, client_key: Optional[str], ref_id: str, reason: str) -> dict:
    """Recorded once so support can see why a customer got nothing; never re-claimed."""
    try:
        store.email_log.insert_one({"dedupe_key": dedupe_key, "kind": kind, "client_key": client_key,
                                    "ref_id": ref_id, "status": "skipped", "reason": reason,
                                    "created_at": store.now(), "updated_at": store.now()})
    except DuplicateKeyError:
        pass
    _log(f"{kind} {ref_id} skipped: {reason}")
    return {"status": "skipped", "reason": reason}


async def deliver(ctx: EmailContext, kind: str, *, to_email: str, to_name: str, dedupe_key: str,
                  params: dict, client: Optional[BrevoEmailClient] = None) -> dict:
    """Send one already-claimed email and record the outcome."""
    template_id = ctx.template_id(kind)
    try:
        brevo = client or BrevoEmailClient(sender_email=ctx.sender_email, sender_name=ctx.sender_name)
        brevo.sender_email, brevo.sender_name = ctx.sender_email, ctx.sender_name
        reply_to = (ctx.settings.replyTo, ctx.sender_name) if ctx.settings.replyTo else None
        recipient = (to_email, to_name) if to_name else to_email
        tags = [f"activlink-{kind}", f"client-{ctx.client_key}"]
        if template_id:
            result = await brevo.send([recipient], params["copy"]["subject"], template_id=template_id,
                                      params=params, reply_to=reply_to, tags=tags)
        else:
            result = await brevo.send([recipient], params["copy"]["subject"], render_fallback_html(params),
                                      params=params, reply_to=reply_to, tags=tags)
    except Exception as exc:  # noqa: BLE001 — BrevoError, a missing API key, anything
        _record(dedupe_key, "failed", error=str(exc)[:300], to_masked=store.mask_email(to_email),
                template_id=template_id)
        _log(f"{kind} to {store.mask_email(to_email)} failed: {str(exc)[:200]}")
        return {"status": "failed"}
    _record(dedupe_key, "sent", brevo_message_id=(result or {}).get("messageId"),
            to_masked=store.mask_email(to_email), template_id=template_id)
    _log(f"{kind} sent to {store.mask_email(to_email)} for {ctx.client_key}")
    return {"status": "sent"}


# ---------------------------------------------------------------------------
# Record loading
# ---------------------------------------------------------------------------

def _oid(value) -> Optional[ObjectId]:
    return ObjectId(value) if isinstance(value, str) and ObjectId.is_valid(value) else (
        value if isinstance(value, ObjectId) else None)


def _date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, str) and value:
        return value[:10]
    return ""


def device_summary(device: dict) -> dict:
    ids = device.get("identifiers") or {}
    return {
        "title": ids.get("title") or "",
        "make": ids.get("make") or "",
        "model": ids.get("model") or "",
        "category": ids.get("category") or "",
    }


def _customer(customer_id) -> dict:
    oid = _oid(customer_id)
    return store.customers.find_one({"_id": oid}, {"name": 1, "email": 1}) or {} if oid else {}


def _cover_name(contract: dict, basket: Optional[dict]) -> str:
    for item in (basket or {}).get("Basket") or []:
        if contract.get("quote_id") and item.get("quote_id") == contract.get("quote_id"):
            return item.get("product_name") or ""
        if item.get("deviceId") == contract.get("device_id") and item.get("product_id") == contract.get("offer_id"):
            return item.get("product_name") or ""
    return contract.get("cover_type") or ""


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------

async def send_registration_confirmation(customer_id: str, device_ids: Iterable[str]) -> dict:
    """After devices are paired to a customer on a journey with no purchase."""
    ids = sorted({d for d in device_ids if isinstance(d, str) and ObjectId.is_valid(d)})
    ref = customer_id
    digest = hashlib.sha256(",".join(ids).encode()).hexdigest()[:24]
    dedupe_key = f"registration:{customer_id}:{digest}"
    try:
        found = list(store.devices.find(
            {"_id": {"$in": [ObjectId(d) for d in ids]}, "registrationParameters.customerId": customer_id},
            {"client": 1, "source": 1, "locale": 1, "identifiers": 1}))
        if not found:
            return _skip(dedupe_key, "registration", None, ref, "no devices paired to customer")
        first = found[0]
        # One email is one tenant's: drop any device another brand owns rather than mix them.
        found = [d for d in found if d.get("client") == first.get("client")]
        client_key = client_key_for(first.get("client"), first.get("source"))
        if not client_key:
            return _skip(dedupe_key, "registration", None, ref, "no ClientKey for device client")
        ctx = await resolve_email_context(client_key, first.get("locale"))
        if not ctx:
            return _skip(dedupe_key, "registration", client_key, ref, "unknown ClientKey")
        if not ctx.enabled_for("registration"):
            return _skip(dedupe_key, "registration", client_key, ref, "disabled for client")
        customer = _customer(customer_id)
        to_email = store.deliverable_email(customer.get("email"))
        if not to_email:
            return _skip(dedupe_key, "registration", client_key, ref, "no deliverable email")
        if not ctx.link_base:
            return _skip(dedupe_key, "registration", client_key, ref, "no link base URL for client")
        if not _claim(dedupe_key, "registration", client_key, ref):
            return {"status": "duplicate"}

        items = []
        for device in found:
            token = cloud_links.create_link("device", str(device["_id"]), ctx.client_id, client_key,
                                            to_email, ctx.locale)
            items.append({**device_summary(device),
                          "cloudUrl": cloud_links.link_url(ctx.link_base, token, ctx.locale)})
        params = build_params(ctx, "registration", name=customer.get("name") or "", items=items)
        return await deliver(ctx, "registration", to_email=to_email, to_name=customer.get("name") or "",
                             dedupe_key=dedupe_key, params=params)
    except Exception as exc:  # noqa: BLE001
        _log(f"registration confirmation for {ref} errored: {type(exc).__name__}: {exc}")
        return {"status": "error"}


async def send_embedded_registration_confirmation(registration_id: str) -> dict:
    """After the embedded ``/register`` call, whose customer details live on the
    ``Registrations`` document rather than on ``Customer``."""
    dedupe_key = f"registration:embedded:{registration_id}"
    try:
        reg = store.registrations.find_one({"_id": _oid(registration_id)},
                                           {"clientkey": 1, "locale": 1, "customer": 1, "devices": 1})
        if not reg:
            return _skip(dedupe_key, "registration", None, registration_id, "registration not found")
        client_key = reg.get("clientkey")
        ctx = await resolve_email_context(client_key, reg.get("locale")) if client_key else None
        if not ctx:
            return _skip(dedupe_key, "registration", client_key, registration_id, "unknown ClientKey")
        if not ctx.enabled_for("registration"):
            return _skip(dedupe_key, "registration", client_key, registration_id, "disabled for client")
        customer = reg.get("customer") or {}
        to_email = store.deliverable_email(customer.get("email"))
        if not to_email:
            return _skip(dedupe_key, "registration", client_key, registration_id, "no deliverable email")
        if not ctx.link_base:
            return _skip(dedupe_key, "registration", client_key, registration_id, "no link base URL for client")
        if not _claim(dedupe_key, "registration", client_key, registration_id):
            return {"status": "duplicate"}

        token = cloud_links.create_link("registration", registration_id, ctx.client_id, client_key,
                                        to_email, ctx.locale)
        url = cloud_links.link_url(ctx.link_base, token, ctx.locale)
        items = []
        for device in reg.get("devices") or []:
            ids = device.get("Identifiers") or {}
            items.append({"title": ids.get("title") or "", "make": ids.get("make") or "",
                          "model": ids.get("model") or "", "category": ids.get("category") or "",
                          "cloudUrl": url})
        params = build_params(ctx, "registration", name=customer.get("name") or "", items=items)
        return await deliver(ctx, "registration", to_email=to_email, to_name=customer.get("name") or "",
                             dedupe_key=dedupe_key, params=params)
    except Exception as exc:  # noqa: BLE001
        _log(f"embedded registration confirmation for {registration_id} errored: {type(exc).__name__}: {exc}")
        return {"status": "error"}


async def send_plan_confirmation(contracts: list[dict], customer_id: str) -> dict:
    """After the Stripe webhook has issued the contracts for one checkout."""
    contracts = [c for c in contracts or [] if c and c.get("order_reference")]
    if not contracts:
        return {"status": "skipped", "reason": "no contracts"}
    order_reference = contracts[0]["order_reference"]
    dedupe_key = f"plan:{order_reference}"
    try:
        device_ids = [c.get("device_id") for c in contracts]
        devices = {str(d["_id"]): d for d in store.devices.find(
            {"_id": {"$in": [o for o in (_oid(x) for x in device_ids) if o]}},
            {"client": 1, "source": 1, "locale": 1, "identifiers": 1})}
        basket_oid = _oid(contracts[0].get("basket_id"))
        basket = store.baskets.find_one({"_id": basket_oid}, {"client": 1, "locale": 1, "Basket": 1}) if basket_oid else None

        first_device = next((devices[d] for d in device_ids if d in devices), None)
        client_id = (first_device or {}).get("client") or (basket or {}).get("client")
        source = (first_device or {}).get("source") or contracts[0].get("source")
        client_key = client_key_for(client_id, source)
        if not client_key:
            return _skip(dedupe_key, "plan", None, order_reference, "no ClientKey for order")
        locale = (basket or {}).get("locale") or (first_device or {}).get("locale")
        ctx = await resolve_email_context(client_key, locale)
        if not ctx:
            return _skip(dedupe_key, "plan", client_key, order_reference, "unknown ClientKey")
        if not ctx.enabled_for("plan"):
            return _skip(dedupe_key, "plan", client_key, order_reference, "disabled for client")
        customer = _customer(customer_id)
        to_email = store.deliverable_email(customer.get("email"))
        if not to_email:
            return _skip(dedupe_key, "plan", client_key, order_reference, "no deliverable email")
        if not ctx.link_base:
            return _skip(dedupe_key, "plan", client_key, order_reference, "no link base URL for client")
        if not _claim(dedupe_key, "plan", client_key, order_reference):
            return {"status": "duplicate"}

        items = []
        for contract in contracts:
            device = devices.get(contract.get("device_id")) or {}
            if device and device.get("client") != ctx.client_id:
                continue
            token = cloud_links.create_link("contract", str(contract["_id"]), ctx.client_id, client_key,
                                            to_email, ctx.locale)
            items.append({**device_summary(device),
                          "coverName": _cover_name(contract, basket),
                          "reference": contract.get("reference") or "",
                          "status": contract.get("status") or "",
                          "startDate": _date(contract.get("start_date")),
                          "endDate": _date(contract.get("end_date")),
                          "cloudUrl": cloud_links.link_url(ctx.link_base, token, ctx.locale)})
        params = build_params(ctx, "plan", name=customer.get("name") or "", items=items)
        params["orderReference"] = order_reference
        return await deliver(ctx, "plan", to_email=to_email, to_name=customer.get("name") or "",
                             dedupe_key=dedupe_key, params=params)
    except Exception as exc:  # noqa: BLE001
        _log(f"plan confirmation for {order_reference} errored: {type(exc).__name__}: {exc}")
        return {"status": "error"}


async def send_otp_email(link: dict, code: str) -> dict:
    dedupe_key = f"otp:{link['_id']}:{uuid.uuid4().hex}"
    ctx = await resolve_email_context(link.get("client_key"), link.get("locale"))
    to_email = store.deliverable_email(link.get("email"))
    if not ctx or not to_email:
        return {"status": "skipped"}
    _claim(dedupe_key, "otp", ctx.client_key, str(link["_id"]))
    params = build_params(ctx, "otp", code=code)
    return await deliver(ctx, "otp", to_email=to_email, to_name="", dedupe_key=dedupe_key, params=params)


def test_sends_in_last_hour(client_key: str) -> int:
    return store.email_log.count_documents({"kind": "test", "client_key": client_key,
                                            "created_at": {"$gte": store.now() - timedelta(hours=1)}})


SAMPLE_ITEMS = [{"title": "Washing machine", "make": "Bosch", "model": "WGG244A9GB", "category": "Laundry",
                 "coverName": "3 year breakdown cover", "reference": "ACT-2026-000123",
                 "startDate": "2026-10-30", "endDate": "2029-10-29"}]


async def send_test_email(client_key: str, kind: str, to_email: str, locale: Optional[str]) -> dict:
    """Portal preview: sample data, current settings, ignoring the enabled toggle."""
    ctx = await resolve_email_context(client_key, locale)
    if not ctx:
        return {"status": "skipped", "reason": "unknown ClientKey"}
    dedupe_key = f"test:{client_key}:{uuid.uuid4().hex}"
    _claim(dedupe_key, "test", client_key, kind)
    sample_url = f"{ctx.link_base or 'https://example.com'}/cloud/sample"
    items = [] if kind == "otp" else [{**SAMPLE_ITEMS[0], "cloudUrl": sample_url}]
    params = build_params(ctx, kind, name="Alex Sample", items=items, code="123456" if kind == "otp" else None)
    return await deliver(ctx, kind, to_email=to_email, to_name="", dedupe_key=dedupe_key, params=params)
