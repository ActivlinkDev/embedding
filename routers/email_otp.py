"""Email verification codes for /my-registrations and /my-contracts.

The SMS flow in ``routers/otp.py`` proves a phone; this proves an email address, so customers
who registered with an email (or who arrive from a confirmation email) can see their records.
Called only by the trusted frontend, which resolves ``clientkey`` from the request host and keeps
the verified address in its signed session cookie.

A code is only *sent* when the address belongs to a customer with devices or contracts on this
client, but the response is the same either way: the endpoint never confirms whether an address
is registered, and cannot be used to email strangers. Requests for addresses that get no email
still count towards the hourly limit, so the limit doesn't reveal it either.
"""
import secrets
import sys
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from routers.comms import links, otp_codes, store
from routers.comms.confirmation import send_otp_email
from routers.customer.create_customer import customer_ids_for_email
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from utils.tenant import client_id_for_key, device_ids_owned_by

# Fail at import (and so skip this router) rather than hash codes without a secret.
otp_codes.secret()

router = APIRouter(tags=["Messaging"])


class EmailOtpRequest(BaseModel):
    clientkey: str = Field(..., min_length=1, description="**Mandatory.** Resolved by the frontend from the request host.")
    email: Optional[str] = Field(None, max_length=254, description="The address to verify. Send this or `link`.")
    link: Optional[str] = Field(None, max_length=128, description="A confirmation-email link token; the code goes to the address it was sent to.")
    locale: Optional[str] = Field(None, max_length=10)

    @model_validator(mode="after")
    def _one_target(self):
        if bool(self.email) == bool(self.link):
            raise ValueError("Send exactly one of email or link")
        return self


class EmailOtpVerify(EmailOtpRequest):
    code: str = Field(..., pattern=r"^\d{6}$")


class LinkResolveRequest(BaseModel):
    clientkey: str = Field(..., min_length=1)
    link: str = Field(..., max_length=128)


def _client_id(clientkey: str) -> str:
    client_id = client_id_for_key(clientkey)
    if not client_id:
        raise HTTPException(400, "Invalid clientkey.")
    return client_id


def _target(body: EmailOtpRequest, client_id: str) -> tuple[Optional[str], Optional[dict]]:
    """The address the code is for, and the link it came from (if any)."""
    if body.link:
        link = links.resolve_link(body.link, client_id)
        return (store.deliverable_email(link.get("email")) if link else None), link
    return store.deliverable_email(body.email), None


def has_records(email: str, client_id: str) -> bool:
    """Whether this address owns anything a verified visit to this client would show."""
    ids = customer_ids_for_email(email)
    if not ids:
        return False
    if store.devices.count_documents({"client": client_id, "registrationParameters.customerId": {"$in": ids}}, limit=1):
        return True
    contracts = store.contracts.find({"customer_id": {"$in": [i for i in ids if isinstance(i, str)]}}, {"device_id": 1})
    return bool(device_ids_owned_by(client_id, [c.get("device_id") for c in contracts]))


@router.post(
    "/otp/email/request",
    summary="Email a verification code for /my-registrations or /my-contracts",
    responses=secured({
        200: json_response("Always this shape, whether or not the address is registered.",
                           {"sent": True, "maskedEmail": "p***@g***.com"}),
        400: error("Unknown `clientkey`.", "Invalid clientkey."),
        429: error("Five codes were already requested for this address in the last hour.", "rate_limited"),
    }),
)
async def request_email_code(body: EmailOtpRequest, _: None = Depends(verify_token)):
    client_id = _client_id(body.clientkey)
    email, link = _target(body, client_id)
    masked = store.mask_email(email) if email else store.mask_email(body.email or "")
    if not email:
        return {"sent": True, "maskedEmail": masked}
    subject = otp_codes.subject_for(email)
    if otp_codes.sends_in_last_hour(subject) >= otp_codes.OTP_MAX_PER_HOUR:
        raise HTTPException(429, "rate_limited")

    if not has_records(email, client_id):
        # Counted like a real send, so the hourly limit behaves the same for every address.
        store.email_otp.insert_one({"subject": subject, "code_hash": secrets.token_hex(32), "attempts": 0,
                                    "superseded": True, "used": True, "created_at": store.now(),
                                    "expires_at": store.now(), "purge_at": store.now() + otp_codes.OTP_TTL * 6})
        return {"sent": True, "maskedEmail": masked}

    code = otp_codes.issue_code(subject)
    locale = body.locale or (link or {}).get("locale")
    result = await send_otp_email(body.clientkey, locale, email, code)
    if result.get("status") != "sent":
        print(f"[EMAIL-OTP] code for {masked} not sent: {result}", file=sys.stderr)
    return {"sent": True, "maskedEmail": masked}


@router.post(
    "/otp/email/verify",
    summary="Check an email verification code",
    responses=secured({
        200: json_response("Correct code. `email` is the verified address, for the session cookie.",
                           {"verified": True, "email": "paul.smith@gmail.com"}),
        400: error("Wrong or expired code.", "incorrect"),
        429: error("Five wrong attempts; request a new code.", "too_many_attempts"),
    }),
)
def verify_email_code(body: EmailOtpVerify, _: None = Depends(verify_token)):
    client_id = _client_id(body.clientkey)
    email, _link = _target(body, client_id)
    if not email:
        raise HTTPException(400, "incorrect")
    try:
        otp_codes.check_code(otp_codes.subject_for(email), body.code)
    except otp_codes.OtpError as exc:
        raise HTTPException(exc.status, exc.code)
    return {"verified": True, "email": email}


@router.post(
    "/email-links/resolve",
    summary="What a confirmation-email link points at",
    responses=secured({
        200: json_response("The link is live for this client.",
                           {"kind": "contract", "refId": "68d0e1f2a3b4c5d6e7f80011", "maskedEmail": "p***@g***.com"}),
        404: error("Unknown, revoked or another client's link.", "Not found"),
    }),
)
def resolve_email_link(body: LinkResolveRequest, _: None = Depends(verify_token)):
    """For the frontend server only. It shows the masked address before verification, and
    uses `refId` to highlight a record only when it is already in the verified customer's own
    results — never to fetch one."""
    link = links.resolve_link(body.link, client_id_for_key(body.clientkey))
    if not link:
        raise HTTPException(404, "Not found")
    return {"kind": link.get("kind"), "refId": str(link.get("ref_id") or ""),
            "maskedEmail": store.mask_email(store.deliverable_email(link.get("email")))}
