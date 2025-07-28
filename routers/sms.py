from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
import os
import requests

from utils.dependencies import verify_token

router = APIRouter(
    prefix="/notify",
    tags=["Notifications"]
)

VOODOO_API_KEY = os.getenv("VOODOO_SMS_API_KEY")
VOODOO_SMS_URL = "https://api.voodoosms.com/sendsms"
VOODOO_SMS_FROM = os.getenv("VOODOO_SMS_FROM", "Activlink")

class SendSmsRequest(BaseModel):
    number: str = Field(..., description="Destination phone number, international format")
    message: str = Field(..., description="Message to send")
    schedule: str = Field(default=None, description="Optional: e.g., '3 weeks'")
    external_reference: str = Field(default=None, description="Optional external reference/id for tracking")

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

@router.post("/send_sms")
def send_sms(
    req: SendSmsRequest,
    _: None = Depends(verify_token)
):
    """
    Send an SMS using VoodooSMS API.
    """
    payload = build_voodoo_payload(req)
    result = send_voodoo_sms(payload)
    return result
