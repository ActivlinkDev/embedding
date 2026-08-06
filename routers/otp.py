from fastapi import APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel, Field
from typing import Dict, Tuple
import os, time, hmac, hashlib, base64, random, requests
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

router = APIRouter(
    prefix="/otp",
    tags=["Messaging"],
)

# --- Configuration ---
OTP_LENGTH = 6
OTP_TTL_SECONDS = 5 * 60          # 5 minutes
RESEND_COOLDOWN_SECONDS = 30      # reuse within 30s
MAX_ATTEMPTS = 5
COOKIE_NAME = "otp_fallback"
COOKIE_SECRET = os.getenv("OTP_COOKIE_SECRET") or os.getenv("LOOKUP_API_KEY")
if not COOKIE_SECRET or COOKIE_SECRET == "changeme-secret":
    raise RuntimeError("OTP_COOKIE_SECRET or LOOKUP_API_KEY must be set to a strong secret")

# Verified customer cookie config
VERIFIED_COOKIE_NAME = os.getenv("VERIFIED_COOKIE_NAME", "verified_customer")
VERIFIED_TTL_SECONDS = int(os.getenv("VERIFIED_TTL_SECONDS", str(24 * 3600)))

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
        if not hmac.compare_digest(_sign(f"{phone}.{code}"), sig):
            return None
        return {"phone": phone, "code": code}
    except Exception:
        return None


def _serialize_verified_cookie(customer_id: str, expiry_ts: int) -> str:
    """Return a signed payload for the verified_customer cookie."""
    payload = f"{customer_id}.{expiry_ts}"
    sig = _sign(payload)
    return f"{payload}.{sig}"

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
    """Where to send the one-time code."""

    phone: str = Field(
        ...,
        description="**Mandatory.** E.164 phone number **with a leading `+`**. Anything else is rejected with `422`.",
        examples=["+447700900123"],
    )
    channel: str | None = Field(
        default="sms",
        description="Delivery channel. `sms` is the only supported value; anything else returns `422`.",
        examples=["sms"],
    )

    model_config = {"json_schema_extra": {"example": {"phone": "+447700900123", "channel": "sms"}}}

class OtpVerifyIn(BaseModel):
    """The code the customer typed in, and the number it was sent to."""

    phone: str = Field(
        ...,
        description="**Mandatory.** The same number the code was requested for — the code is stored against it.",
        examples=["+447700900123"],
    )
    code: str = Field(..., description="**Mandatory.** The code from the SMS.", examples=["482913"])
    channel: str | None = Field(default="sms", description="Must match the request channel. `sms` only.", examples=["sms"])

    model_config = {"json_schema_extra": {"example": {"phone": "+447700900123", "code": "482913", "channel": "sms"}}}

@router.post(
    "/request",
    summary="Send a one-time code by SMS",
    response_description="Confirmation, the masked destination, and whether an existing code was reused.",
    responses=secured({
        200: json_response(
            "A code is now active for this number.",
            {"success": True, "destination_masked": "+44 ***** **0123", "reused": False},
        ),
        422: error("`phone` has no leading `+`, or `channel` is not `sms`.", "invalid phone format"),
        500: error("The SMS gateway is not configured or could not be reached.", "SMS API key not configured"),
    }),
)
def request_otp(req: OtpRequestIn, response: Response, _: None = Depends(verify_token)):
    """
    Send a one-time code to a phone number.

    The code is valid for **5 minutes** and allows **5 verification attempts**. Requesting again
    within **30 seconds** does not send a second SMS — the existing code stays valid and the
    response comes back with `reused: true`. After that window a fresh code is generated and the
    previous one stops working.

    A signed `otp_fallback` cookie is also set as a delivery fallback, so `POST /otp/verify` can
    still succeed if the server-side record has been lost.

    `phone` is mandatory and **must be E.164 with a leading `+`**; `channel` defaults to `sms`,
    the only supported value. `destination_masked` is safe to display back to the customer.

    Note that codes are held **in process memory**, so a restart or a second replica invalidates
    outstanding codes — the cookie fallback is what covers that case.
    """
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

@router.post(
    "/verify",
    summary="Check a one-time code",
    response_description="Confirmation that the code was accepted.",
    responses=secured({
        200: json_response(
            "The code was correct. `fallback: \"cookie\"` means it was matched against the "
            "signed cookie because the server-side record was gone.",
            {"success": True},
        ),
        400: error(
            "The code was rejected. `detail` says why: `not_found`, `expired`, "
            "`too_many_attempts` or `invalid_code`.",
            "invalid_code",
        ),
        422: error("`channel` is not `sms`.", "unsupported channel"),
    }),
)
def verify_otp(req: OtpVerifyIn, request: Request, response: Response, _: None = Depends(verify_token)):
    """
    Check the code a customer entered.

    All three inputs must line up: the code is stored against the `phone` and `channel` it was
    requested for. On success the `otp_fallback` cookie is cleared and the code is consumed.

    **Failure reasons** arrive as `400` with `detail` set to one of:

    | `detail` | Meaning |
    | --- | --- |
    | `not_found` | No code outstanding for this number — never requested, or already used |
    | `expired` | More than 5 minutes since it was issued |
    | `too_many_attempts` | 5 wrong guesses; request a new code |
    | `invalid_code` | Wrong code, attempt counted |

    If the server-side record is missing but the request carries a valid signed `otp_fallback`
    cookie matching this phone and code, verification succeeds with `fallback: "cookie"`. That is
    the normal path after a restart, since codes live in process memory.

    Verifying does **not** by itself sign the customer in — follow with
    `POST /customer/mark-verified` to issue the verified-customer cookie.
    """
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
                # mark verified in a signed cookie
                try:
                    # derive a customer id lookup is not available here; caller should set this cookie after authenticate flow
                    # We will not guess customer_id here; return fallback success and let authenticate flow set verified cookie.
                    response.delete_cookie(COOKIE_NAME)
                    return {"success": True, "fallback": "cookie"}
                except Exception:
                    response.delete_cookie(COOKIE_NAME)
                    return {"success": True, "fallback": "cookie"}

    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["reason"])

    # On successful verification we set a signed verified_customer cookie. The customer id is not part of OTP request
    # flow; this should be set by the caller (authenticate endpoint) which knows the customer_id. Here we just return
    # success; higher-level callers (authenticate_verified flow) should set the verified cookie with the customer id.
    response.delete_cookie(COOKIE_NAME)
    return {"success": True}
