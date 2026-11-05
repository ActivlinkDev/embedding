"""Portal admin: per-ClientKey email settings and test sends.

Like the rest of ``/portal``, the frontend checks the portal session and that the ClientKey is
one of the signed-in user's before calling; ``verify_token`` additionally rejects a ClientKey
outside the calling credential's binding.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from routers.comms import store
from routers.comms.confirmation import TEST_SENDS_PER_HOUR, send_test_email, test_sends_in_last_hour
from routers.comms.email_config import EmailSettings, allowed_sender_domains, client_key_doc, default_template_id, load_settings
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

router = APIRouter(prefix="/portal", tags=["Portal"])


def _require_client(client_key: str) -> dict:
    doc = client_key_doc(client_key)
    if not doc:
        raise HTTPException(404, "Client not found")
    return doc


def _response(client_key: str, settings: EmailSettings) -> dict:
    return {
        "clientKey": client_key,
        "settings": settings.model_dump(),
        "allowedSenderDomains": sorted(allowed_sender_domains()),
        "defaultTemplateIds": {kind: default_template_id(kind) for kind in ("registration", "plan", "otp")},
    }


@router.get(
    "/email-config",
    summary="Read the email settings for a ClientKey",
    responses=secured({
        200: json_response("Stored settings, or the defaults (all emails off) if none are saved.", {
            "clientKey": "acme_uk_live",
            "settings": {"enabled": {"registration": True, "plan": True}, "senderEmail": None,
                         "senderName": "Acme", "replyTo": None,
                         "templateIds": {"registration": None, "plan": 12, "otp": None},
                         "logoUrl": None, "primaryColour": "#0B5FFF", "buttonColour": None, "footerText": None},
            "allowedSenderDomains": ["activlink.io"],
            "defaultTemplateIds": {"registration": 3, "plan": 4, "otp": 5}}),
        404: error("No ClientKey matches `clientkey`.", "Client not found"),
    }),
)
def get_email_config(clientkey: str = Query(..., description="**Mandatory.**"), _: None = Depends(verify_token)):
    _require_client(clientkey)
    return _response(clientkey, load_settings(clientkey))


class UpdateEmailConfigRequest(BaseModel):
    clientKey: str = Field(..., min_length=1, max_length=100)
    settings: EmailSettings


@router.put(
    "/email-config",
    summary="Save the email settings for a ClientKey",
    responses=secured({
        200: json_response("Saved.", {"clientKey": "acme_uk_live", "settings": {}}),
        404: error("No ClientKey matches `clientKey`.", "Client not found"),
        422: error("A field failed validation, e.g. a sender on a domain not verified in Brevo.", "..."),
    }),
)
def update_email_config(body: UpdateEmailConfigRequest, _: None = Depends(verify_token)):
    """Replaces the email settings for this ClientKey only. Branding in `ClientKey.Styles` is
    not touched; blank logo and colour fields fall back to it when sending."""
    _require_client(body.clientKey)
    store.email_config.update_one(
        {"client_key": body.clientKey},
        {"$set": {"settings": body.settings.model_dump(), "updated_at": store.now()},
         "$setOnInsert": {"client_key": body.clientKey, "created_at": store.now()}},
        upsert=True)
    return _response(body.clientKey, body.settings)


class TestEmailRequest(BaseModel):
    clientKey: str = Field(..., min_length=1, max_length=100)
    kind: Literal["registration", "plan", "otp"]
    to: str = Field(..., max_length=254)
    locale: Optional[str] = Field(None, max_length=10, pattern=r"^[a-z]{2}(_[A-Z]{2})?$")


@router.post(
    "/email-config/test",
    summary="Send a sample email with the saved settings",
    responses=secured({
        200: json_response("Sent.", {"status": "sent"}),
        404: error("No ClientKey matches `clientKey`.", "Client not found"),
        422: error("`to` is not a valid address.", "Invalid recipient"),
        429: error("Ten test emails were already sent for this client in the last hour.", "Too many test emails"),
        502: error("Brevo rejected the send. Check the template id and sender.", "send_failed"),
    }),
)
async def send_test(body: TestEmailRequest, _: None = Depends(verify_token)):
    """Uses sample product data and ignores the enabled toggles, so a template can be checked
    before it goes live. Limited per client because the recipient is free text."""
    _require_client(body.clientKey)
    to = store.deliverable_email(body.to)
    if not to:
        raise HTTPException(422, "Invalid recipient")
    if test_sends_in_last_hour(body.clientKey) >= TEST_SENDS_PER_HOUR:
        raise HTTPException(429, "Too many test emails")
    result = await send_test_email(body.clientKey, body.kind, to, body.locale)
    if result.get("status") != "sent":
        raise HTTPException(502, "send_failed")
    return result
