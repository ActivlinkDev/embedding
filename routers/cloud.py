"""Cloud ID page API: the record behind a confirmation-email link.

Called only by the trusted frontend, which resolves ``clientkey`` from the request host — never
from the browser — and keeps the access grant in an httpOnly cookie.

- ``GET /cloud/summary``: non-personal summary (product, cover, dates, masked email).
- ``POST /cloud/otp/request``: email a 6-digit code to the address the link was sent to.
- ``POST /cloud/otp/verify``: exchange a correct code for a 30-minute grant.
- ``GET /cloud/detail``: personal details, with ``X-Cloud-Grant``.

The link token travels in the ``X-Cloud-Token`` header, not the path, so it stays out of
access logs.

Every route answers 404 for an unknown, revoked or other-tenant token, so a token opened on
another client's storefront reveals nothing — not even that it exists.
"""
import time
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from routers.comms import cloud_links, store
from routers.comms.confirmation import _cover_name, _date, device_summary, send_otp_email
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from utils.tenant import client_id_for_key

# Fail at import (and so skip this router) rather than serve verification without a secret.
cloud_links._secret()

router = APIRouter(prefix="/cloud", tags=["Customers"])

_CLIENTKEY = Query(..., description="**Mandatory.** The tenant, resolved by the frontend from the request host.")
_TOKEN = Header(..., alias="X-Cloud-Token", description="**Mandatory.** The token from the email link.")


def _link(token: str, clientkey: str) -> dict:
    link = cloud_links.resolve_link(token, client_id_for_key(clientkey))
    if not link:
        raise HTTPException(404, "Not found")
    return link


def _oid(value) -> Optional[ObjectId]:
    return ObjectId(value) if isinstance(value, str) and ObjectId.is_valid(value) else None


def _device(device_id, client_id: str) -> Optional[dict]:
    oid = _oid(device_id)
    if not oid:
        return None
    return store.devices.find_one(
        {"_id": oid, "client": client_id},
        {"identifiers": 1, "uniqueParameters": 1, "registrationParameters": 1, "registeredAt": 1,
         "receipt.uploadedAt": 1})


def _load(link: dict) -> list[dict]:
    """[(summary item, detail item)] for the link. Every lookup is re-scoped to the link's
    tenant, so a link can never surface another client's record."""
    client_id = link["client_id"]
    kind = link.get("kind")
    out = []
    if kind == "device":
        device = _device(link.get("ref_id"), client_id)
        if device:
            out.append(_device_pair(device))
    elif kind == "contract":
        contract = store.contracts.find_one({"_id": _oid(link.get("ref_id"))}) if _oid(link.get("ref_id")) else None
        device = _device((contract or {}).get("device_id"), client_id) if contract else None
        # Contracts carry no reliable tenant of their own; the device they cover is the authority.
        if contract and device:
            basket_oid = _oid(contract.get("basket_id"))
            basket = store.baskets.find_one({"_id": basket_oid}, {"Basket": 1}) if basket_oid else None
            summary, detail = _device_pair(device)
            summary["cover"] = {
                "name": _cover_name(contract, basket),
                "reference": contract.get("reference") or "",
                "status": contract.get("status") or "",
                "startDate": _date(contract.get("start_date")),
                "endDate": _date(contract.get("end_date")),
            }
            detail["cover"] = {
                "price": contract.get("price"),
                "currency": contract.get("currency"),
                "billing": contract.get("billing"),
                "documentUrl": contract.get("document_url") or None,
                "ipidUrl": contract.get("ipid_url") or None,
            }
            detail["customerId"] = contract.get("customer_id")
            out.append((summary, detail))
    elif kind == "registration":
        reg_oid = _oid(link.get("ref_id"))
        reg = store.registrations.find_one({"_id": reg_oid}, {"clientkey": 1, "devices": 1, "customer.name": 1,
                                                              "registered_at": 1}) if reg_oid else None
        if reg and client_id_for_key(reg.get("clientkey")) == client_id:
            for device in reg.get("devices") or []:
                ids = device.get("Identifiers") or {}
                unique = device.get("Unique_Parameters") or {}
                summary = {"title": ids.get("title") or "", "make": ids.get("make") or "",
                           "model": ids.get("model") or "", "category": ids.get("category") or "",
                           "registeredAt": _date(device.get("registered_at") or reg.get("registered_at"))}
                detail = {"serial": store.mask_identifier(unique.get("serial")),
                          "imei": store.mask_identifier(unique.get("imei")),
                          "mac": store.mask_identifier(unique.get("MAC") or unique.get("mac")),
                          "purchaseDate": _date(unique.get("purchaseDate") or unique.get("purchase_date")),
                          "purchasePrice": unique.get("price"), "currency": unique.get("currency"),
                          "receiptOnFile": None, "customerName": (reg.get("customer") or {}).get("name")}
                out.append((summary, detail))
    return out


def _device_pair(device: dict) -> tuple[dict, dict]:
    reg = device.get("registrationParameters") or {}
    unique = device.get("uniqueParameters") or {}
    summary = {**device_summary(device), "registeredAt": _date(device.get("registeredAt"))}
    detail = {
        "serial": store.mask_identifier(unique.get("serial")),
        "imei": store.mask_identifier(unique.get("imei")),
        "mac": store.mask_identifier(unique.get("MAC") or unique.get("mac")),
        "purchaseDate": _date(reg.get("purchaseDate")),
        "purchasePrice": reg.get("price"),
        "currency": reg.get("currency"),
        "receiptOnFile": bool(device.get("receipt")),
        "customerId": reg.get("customerId"),
    }
    return summary, detail


def _customer_name(customer_id) -> Optional[str]:
    oid = _oid(customer_id)
    doc = store.customers.find_one({"_id": oid}, {"name": 1}) if oid else None
    return (doc or {}).get("name") or None


@router.get(
    "/summary",
    summary="Non-personal summary of the record behind a confirmation-email link",
    responses=secured({
        200: json_response("The link is live for this tenant.", {
            "kind": "contract", "locale": "en_GB", "maskedEmail": "p***@g***.com", "canVerify": True,
            "items": [{"title": "Washing machine", "make": "Bosch", "model": "WGG244A9GB", "category": "Laundry",
                       "registeredAt": "2026-10-30",
                       "cover": {"name": "3 year cover", "reference": "ACT-2026-000123", "status": "ACTIVE",
                                 "startDate": "2026-10-30", "endDate": "2029-10-29"}}]}),
        404: error("Unknown, revoked or another tenant's link.", "Not found"),
    }),
)
def cloud_summary(token: str = _TOKEN, clientkey: str = _CLIENTKEY, _: None = Depends(verify_token)):
    """The summary never includes names, contact details, identifiers or prices."""
    link = _link(token, clientkey)
    items = [summary for summary, _detail in _load(link)]
    if not items:
        raise HTTPException(404, "Not found")
    email = store.deliverable_email(link.get("email"))
    return {"kind": link.get("kind"), "locale": link.get("locale"), "items": items,
            "maskedEmail": store.mask_email(email), "canVerify": bool(email)}


class OtpVerifyBody(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


@router.post(
    "/otp/request",
    summary="Email a verification code for a Cloud ID link",
    responses=secured({
        200: json_response("A code was sent to the link's email address.", {"sent": True, "maskedEmail": "p***@g***.com"}),
        404: error("Unknown, revoked or another tenant's link.", "Not found"),
        409: error("The link has no deliverable email to verify against.", "no_email"),
        429: error("Five codes were already requested for this link in the last hour.", "rate_limited"),
        502: error("The email could not be sent.", "send_failed"),
    }),
)
async def cloud_otp_request(token: str = _TOKEN, clientkey: str = _CLIENTKEY, _: None = Depends(verify_token)):
    link = _link(token, clientkey)
    email = store.deliverable_email(link.get("email"))
    if not email:
        raise HTTPException(409, "no_email")
    try:
        code = cloud_links.issue_code(link)
    except cloud_links.OtpError as exc:
        raise HTTPException(exc.status, exc.code)
    result = await send_otp_email(link, code)
    if result.get("status") != "sent":
        raise HTTPException(502, "send_failed")
    return {"sent": True, "maskedEmail": store.mask_email(email)}


@router.post(
    "/otp/verify",
    summary="Exchange a verification code for a short-lived access grant",
    responses=secured({
        200: json_response("Correct code. Forward `grant` as `X-Cloud-Grant` until `expiresAt` (epoch seconds).",
                           {"ok": True, "grant": "<opaque>", "expiresAt": 1793356800}),
        400: error("Wrong or expired code.", "incorrect"),
        404: error("Unknown, revoked or another tenant's link.", "Not found"),
        429: error("Five wrong attempts; request a new code.", "too_many_attempts"),
    }),
)
def cloud_otp_verify(body: OtpVerifyBody, token: str = _TOKEN, clientkey: str = _CLIENTKEY,
                     _: None = Depends(verify_token)):
    link = _link(token, clientkey)
    try:
        cloud_links.check_code(link, body.code)
    except cloud_links.OtpError as exc:
        raise HTTPException(exc.status, exc.code)
    grant, expires = cloud_links.issue_grant(link, int(time.time()))
    return {"ok": True, "grant": grant, "expiresAt": expires}


@router.get(
    "/detail",
    summary="Personal details behind a Cloud ID link (after email verification)",
    responses=secured({
        200: json_response("Identifiers are masked to their last four characters.", {
            "customerName": "Paul Smith",
            "items": [{"title": "Washing machine", "serial": "••••••1203", "imei": None, "mac": None,
                       "purchaseDate": "2026-10-01", "purchasePrice": 449.99, "currency": "GBP",
                       "receiptOnFile": True,
                       "cover": {"price": 89.0, "currency": "GBP", "billing": "ONE_OFF",
                                 "documentUrl": None, "ipidUrl": None}}]}),
        401: error("Missing, expired or invalid grant.", "verification_required"),
        404: error("Unknown, revoked or another tenant's link.", "Not found"),
    }),
)
def cloud_detail(token: str = _TOKEN, clientkey: str = _CLIENTKEY,
                 x_cloud_grant: Optional[str] = Header(None),
                 _: None = Depends(verify_token)):
    link = _link(token, clientkey)
    if not cloud_links.grant_valid(link, x_cloud_grant, int(time.time())):
        raise HTTPException(401, "verification_required")
    items, name = [], None
    for summary, detail in _load(link):
        customer_id = detail.pop("customerId", None)
        name = name or detail.pop("customerName", None) or _customer_name(customer_id)
        detail.pop("customerName", None)
        items.append({**summary, **detail, "cover": {**summary.get("cover", {}), **detail.get("cover", {})}
                      if "cover" in summary else None})
    if not items:
        raise HTTPException(404, "Not found")
    return {"customerName": name, "items": items}
