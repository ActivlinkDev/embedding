from fastapi import APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel, Field
from typing import Dict, Tuple
import os, time, hmac, hashlib, base64, random, requests
from utils.dependencies import verify_token

router = APIRouter(
    prefix="/otp",
    tags=["OTP"],
)

# --- Configuration ---
OTP_LENGTH = 6
OTP_TTL_SECONDS = 5 * 60          # 5 minutes
RESEND_COOLDOWN_SECONDS = 30      # reuse within 30s
MAX_ATTEMPTS = 5
COOKIE_NAME = "otp_fallback"
COOKIE_SECRET = os.getenv("OTP_COOKIE_SECRET", os.getenv("LOOKUP_API_KEY", "changeme-secret"))

VOODOO_API_KEY = os.getenv("VOODOO_SMS_API_KEY")
VOODOO_SMS_URL = "https://api.voodoosms.com/sendsms"
VOODOO_SMS_FROM = os.getenv("VOODOO_SMS_FROM", "Activlink")

# In-memory OTP storage: key = (phone, channel)
_store: Dict[Tuple[str, str], Dict] = {}

def _now_ms() -> int:
    return int(time.time() * 1000)

def _generate_code() -> str:
    return "".join(str(random.randint(0,9)) for _ in range(OTP_LENGTH))

def _get_record(phone: str, channel: str):
    rec = _store.get((phone, channel))
    if not rec:
        return None
    if _now_ms() - rec["createdAt"] > OTP_TTL_SECONDS * 1000:
        _store.pop((phone, channel), None)
        return None
    return rec

def _save_code(phone: str, channel: str, code: str):
    _store[(phone, channel)] = {"code": code, "createdAt": _now_ms(), "attempts": 0}

def _verify(phone: str, channel: str, code: str):
    rec = _get_record(phone, channel)
    if not rec:
        return {"ok": False, "reason": "not_found"}
    if _now_ms() - rec["createdAt"] > OTP_TTL_SECONDS * 1000:
        _store.pop((phone, channel), None)
        return {"ok": False, "reason": "expired"}
    if rec["attempts"] >= MAX_ATTEMPTS:
        return {"ok": False, "reason": "too_many_attempts"}
    if rec["code"] != code:
        rec["attempts"] += 1
        return {"ok": False, "reason": "invalid_code"}
    _store.pop((phone, channel), None)
    return {"ok": True}

def mask_destination(phone: str) -> str:
    if len(phone) <= 6:
        return phone[:2] + "****"
    return phone[:4] + "****" + phone[-2:]

# Signed cookie fallback helpers

def _sign(value: str) -> str:
    sig = hmac.new(COOKIE_SECRET.encode(), value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")

def serialize_cookie(code: str, phone: str) -> str:
    payload = f"{phone}.{code}"
    sig = _sign(payload)
    return f"{payload}.{sig}"

def parse_cookie(raw: str):
    try:
        parts = raw.split('.')
        if len(parts) < 3:
            return None
        phone = '.'.join(parts[:-2])
        code = parts[-2]
        sig = parts[-1]
        if _sign(f"{phone}.{code}") != sig:
            return None
        return {"phone": phone, "code": code}
    except Exception:
        return None

# SMS sending

def _send_sms(number: str, message: str):
    if not VOODOO_API_KEY:
        raise HTTPException(status_code=500, detail="SMS API key not configured")
    headers = {"Authorization": f"Bearer {VOODOO_API_KEY}"}
    payload = {"to": number, "from": VOODOO_SMS_FROM, "msg": message}
    try:
        r = requests.post(VOODOO_SMS_URL, json=payload, headers=headers, timeout=10)
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=f"SMS upstream error: {r.text}")
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"SMS send failed: {e}")

# Schemas
class OtpRequestIn(BaseModel):
    phone: str = Field(..., description="E.164 phone number with leading +")
    channel: str | None = Field(default="sms")

class OtpVerifyIn(BaseModel):
    phone: str
    code: str
    channel: str | None = Field(default="sms")

@router.post("/request")
def request_otp(req: OtpRequestIn, response: Response, _: None = Depends(verify_token)):
    phone = req.phone.strip()
    channel = req.channel or "sms"
    if channel != "sms":
        raise HTTPException(status_code=422, detail="unsupported channel")
    if not phone.startswith('+'):
        raise HTTPException(status_code=422, detail="invalid phone format")

    existing = _get_record(phone, channel)
    now = _now_ms()
    reused = False
    if existing and (now - existing["createdAt"]) < RESEND_COOLDOWN_SECONDS * 1000:
        code = existing["code"]
        reused = True
    else:
        code = _generate_code()
        _save_code(phone, channel, code)

    if not reused:
        _send_sms(phone, f"Your validation code is: {code}")

    try:
        cookie_val = serialize_cookie(code, phone)
        response.set_cookie(
            key=COOKIE_NAME,
            value=cookie_val,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=OTP_TTL_SECONDS
        )
    except Exception:
        pass

    return {"success": True, "destination_masked": mask_destination(phone), "reused": reused}

@router.post("/verify")
def verify_otp(req: OtpVerifyIn, request: Request, response: Response, _: None = Depends(verify_token)):
    phone = req.phone.strip()
    code = req.code.strip()
    channel = req.channel or "sms"
    if channel != "sms":
        raise HTTPException(status_code=422, detail="unsupported channel")

    result = _verify(phone, channel, code)

    if not result["ok"] and result["reason"] == "not_found":
        raw_cookie = request.cookies.get(COOKIE_NAME)
        if raw_cookie:
            parsed = parse_cookie(raw_cookie)
            if parsed and parsed["phone"] == phone and parsed["code"] == code:
                response.delete_cookie(COOKIE_NAME)
                return {"success": True, "fallback": "cookie"}

    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["reason"])

    response.delete_cookie(COOKIE_NAME)
    return {"success": True}
