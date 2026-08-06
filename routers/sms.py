from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
import os
import requests

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

router = APIRouter(
    prefix="/notify",
    tags=["Messaging"]
)

VOODOO_API_KEY = os.getenv("VOODOO_SMS_API_KEY")
VOODOO_SMS_URL = "https://api.voodoosms.com/sendsms"
VOODOO_SMS_FROM = os.getenv("VOODOO_SMS_FROM", "Activlink")

class SendSmsRequest(BaseModel):
    """A message to send through the SMS gateway."""

    number: str = Field(
        ...,
        description="**Mandatory.** Destination number in international format.",
        examples=["+447700900123"],
    )
    message: str = Field(
        ...,
        description="**Mandatory.** Message body. Long messages are split into multiple parts by the gateway and billed accordingly.",
        examples=["Your Activlink cover is now active. Ref ORD-2026-00918."],
    )
    schedule: str = Field(
        default=None,
        description="Send later instead of now, as a relative phrase the gateway understands, e.g. `3 weeks`.",
        examples=["3 weeks"],
    )
    external_reference: str = Field(
        default=None,
        description="Your own reference, passed to the gateway for tracking and reconciliation.",
        examples=["ORD-2026-00918"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "number": "+447700900123",
                "message": "Your Activlink cover is now active. Ref ORD-2026-00918.",
                "external_reference": "ORD-2026-00918",
            }
        }
    }

def build_voodoo_payload(data: SendSmsRequest) -> dict:
    payload = {
        "to": data.number,
        "from": VOODOO_SMS_FROM,
        "msg": data.message,
    }
    if data.schedule:
        payload["schedule"] = data.schedule
    if data.external_reference:
        payload["external_reference"] = data.external_reference
    return payload

def send_voodoo_sms(payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {VOODOO_API_KEY}",
    }
    try:
        resp = requests.post(VOODOO_SMS_URL, json=payload, headers=headers, timeout=10)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"VoodooSMS error: {resp.text}")
        return resp.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"SMS gateway error: {e}")

@router.post(
    "/send_sms",
    summary="Send an SMS through the gateway",
    response_description="The gateway's response, passed through unchanged.",
    responses=secured({
        200: json_response(
            "The gateway accepted the message. The body is **whatever the provider returned** — "
            "its shape is not defined by this API.",
            {"count": 1, "dest": "447700900123", "credits": 1, "message_id": "6f1c2d3e4a5b"},
        ),
        500: error("The SMS gateway could not be reached.", "SMS gateway error: ..."),
    }),
)
def send_sms(
    req: SendSmsRequest,
    _: None = Depends(verify_token)
):
    """
    Send an arbitrary SMS through the VoodooSMS gateway.

    `number` and `message` are mandatory. Set `schedule` to send later, and
    `external_reference` to tag the message for your own reconciliation.

    **The response is the gateway's own**, forwarded unchanged, so treat its fields as
    provider-defined rather than part of this API's contract. A `4xx` from the gateway is
    passed through with its original status code; a network failure becomes `500`.

    A `200` means the gateway **accepted** the message, not that it was delivered.

    This sends whatever text it is given to whatever number it is given — it is not the OTP
    flow. Use `POST /otp/request` for verification codes.
    """
    payload = build_voodoo_payload(req)
    result = send_voodoo_sms(payload)
    return result
