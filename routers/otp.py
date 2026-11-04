from fastapi import APIRouter, HTTPException, Depends, Response
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import logging, os, time, hmac, hashlib, base64, secrets, requests
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from utils.mongo import require_client
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/otp",
    tags=["Messaging"],
)

# --- Configuration ---
OTP_LENGTH = 6
OTP_TTL_SECONDS = 5 * 60          # 5 minutes
RESEND_COOLDOWN_SECONDS = 30      # reuse within 30s
MAX_ATTEMPTS = 5
# Per-number send cap, so the endpoint cannot be used to run up SMS costs or flood a handset.
SEND_WINDOW_SECONDS = 60 * 60
MAX_SENDS_PER_WINDOW = int(os.getenv("OTP_MAX_SENDS_PER_HOUR", "5"))
# Name of the retired cookie that used to carry the code to the browser. Still cleared on
# verify so browsers holding one from before this change drop it; it is never read.
LEGACY_COOKIE_NAME = "otp_fallback"
CODE_SECRET = os.getenv("OTP_COOKIE_SECRET") or os.getenv("LOOKUP_API_KEY")
if not CODE_SECRET or CODE_SECRET == "changeme-secret":
    raise RuntimeError("OTP_COOKIE_SECRET or LOOKUP_API_KEY must be set to a strong secret")

# Verified customer cookie config
VERIFIED_COOKIE_NAME = os.getenv("VERIFIED_COOKIE_NAME", "verified_customer")
VERIFIED_TTL_SECONDS = int(os.getenv("VERIFIED_TTL_SECONDS", str(24 * 3600)))

VOODOO_API_KEY = os.getenv("VOODOO_SMS_API_KEY")
VOODOO_SMS_URL = "https://api.voodoosms.com/sendsms"
VOODOO_SMS_FROM = os.getenv("VOODOO_SMS_FROM", "Activlink")

# Challenges live in MongoDB, not process memory, so every replica sees the same state and a
# restart does not lose outstanding codes. Only an HMAC of the code is stored; the code itself
# goes to the handset and nowhere else — in particular never back to the requester.
_db = require_client()["Activlink"]
otp_collection = _db["OtpChallenges"]
_index_ready = False


def _ensure_indexes() -> None:
    """TTL index so expired challenges are purged. Best effort: expiry is enforced in code too."""
    global _index_ready
    if _index_ready:
        return
    try:
        otp_collection.create_index("purgeAt", expireAfterSeconds=0)
        _index_ready = True
    except Exception as exc:  # pragma: no cover - depends on the live database
        logger.warning("[OTP] could not ensure TTL index: %s", exc)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _generate_code() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(OTP_LENGTH))


def _challenge_id(phone: str, channel: str) -> str:
    return f"{channel}:{phone}"


def _hash_code(phone: str, channel: str, code: str) -> str:
    msg = f"{channel}|{phone}|{code}".encode()
    return hmac.new(CODE_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def _get_record(phone: str, channel: str):
    """The live (unexpired, unconsumed) challenge for this number, or None."""
    rec = otp_collection.find_one({"_id": _challenge_id(phone, channel)})
    if not rec or rec.get("consumed"):
        return None
    if _now_ms() - rec.get("createdAt", 0) > OTP_TTL_SECONDS * 1000:
        return None
    return rec


def issue_code(phone: str, channel: str = "sms") -> bool:
    """Create (or, inside the resend cooldown, keep) the challenge for `phone` and send the SMS.

    Returns True when the existing challenge was kept and no SMS was sent. Raises 429 once the
    per-number send cap for the current window is reached.
    """
    _ensure_indexes()
    cid = _challenge_id(phone, channel)
    now = _now_ms()

    prior = otp_collection.find_one({"_id": cid})
    if (prior and not prior.get("consumed")
            and (now - prior.get("createdAt", 0)) < RESEND_COOLDOWN_SECONDS * 1000):
        return True

    window_start = (prior or {}).get("sendWindowStart") or 0
    send_count = (prior or {}).get("sendCount") or 0
    if now - window_start >= SEND_WINDOW_SECONDS * 1000:
        window_start, send_count = now, 0
    if send_count >= MAX_SENDS_PER_WINDOW:
        raise HTTPException(status_code=429, detail="too_many_requests")

    code = _generate_code()
    nonce = secrets.token_hex(16)
    new_doc = {
        "_id": cid,
        "phone": phone,
        "channel": channel,
        "codeHash": _hash_code(phone, channel, code),
        "nonce": nonce,
        "createdAt": now,
        "attempts": 0,
        "consumed": False,
        "sendWindowStart": window_start,
        "sendCount": send_count + 1,
        # Kept past the code's own TTL so the send-rate window survives until it lapses.
        "purgeAt": datetime.fromtimestamp((now / 1000) + max(OTP_TTL_SECONDS, SEND_WINDOW_SECONDS), tz=timezone.utc),
    }

    # Compare-and-swap on the challenge we read: if a concurrent request replaced it first, we
    # lose and send nothing, so two racing requests cannot both pass the cap or both send.
    if prior is None:
        try:
            otp_collection.insert_one(new_doc)
        except DuplicateKeyError:
            return True
    else:
        swapped = otp_collection.replace_one({"_id": cid, "nonce": prior.get("nonce")}, new_doc)
        if swapped.modified_count != 1:
            return True

    try:
        _send_sms(phone, f"Your validation code is: {code}")
    except Exception:
        # Retire the undelivered challenge so an immediate retry sends a fresh code instead of
        # landing in the resend cooldown. The send still counts against the window.
        otp_collection.update_one({"_id": cid, "nonce": nonce}, {"$set": {"consumed": True}})
        raise
    return False


def _verify(phone: str, channel: str, code: str):
    cid = _challenge_id(phone, channel)
    rec = otp_collection.find_one({"_id": cid})
    if not rec or rec.get("consumed"):
        return {"ok": False, "reason": "not_found"}
    if _now_ms() - rec.get("createdAt", 0) > OTP_TTL_SECONDS * 1000:
        return {"ok": False, "reason": "expired"}
    if rec.get("attempts", 0) >= MAX_ATTEMPTS:
        return {"ok": False, "reason": "too_many_attempts"}

    nonce = rec.get("nonce")
    if not hmac.compare_digest(rec.get("codeHash", ""), _hash_code(phone, channel, code)):
        otp_collection.update_one({"_id": cid, "nonce": nonce}, {"$inc": {"attempts": 1}})
        return {"ok": False, "reason": "invalid_code"}

    # Consume atomically: of two concurrent submissions of the right code only one matches
    # `consumed: False`, so a code can never verify twice.
    result = otp_collection.update_one(
        {"_id": cid, "nonce": nonce, "consumed": False, "attempts": {"$lt": MAX_ATTEMPTS}},
        {"$set": {"consumed": True, "consumedAt": _now_ms()}},
    )
    if result.modified_count != 1:
        return {"ok": False, "reason": "not_found"}
    return {"ok": True}

def mask_destination(phone: str) -> str:
    if len(phone) <= 6:
        return phone[:2] + "****"
    return phone[:4] + "****" + phone[-2:]


def _sign(value: str) -> str:
    sig = hmac.new(CODE_SECRET.encode(), value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


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
    except requests.RequestException as e:
        logger.warning("[OTP] SMS send failed: %s", type(e).__name__)
        raise HTTPException(status_code=502, detail="SMS send failed")
    if r.status_code >= 400:
        logger.warning("[OTP] SMS upstream returned %s", r.status_code)
        raise HTTPException(status_code=502, detail="SMS upstream error")

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
        429: error("Too many codes have been sent to this number in the last hour.", "too_many_requests"),
        500: error("The SMS gateway is not configured.", "SMS API key not configured"),
        502: error("The SMS gateway could not be reached or rejected the message.", "SMS send failed"),
    }),
)
def request_otp(req: OtpRequestIn, _: None = Depends(verify_token)):
    """
    Send a one-time code to a phone number.

    The code is valid for **5 minutes** and allows **5 verification attempts**. Requesting again
    within **30 seconds** does not send a second SMS — the existing code stays valid and the
    response comes back with `reused: true`. After that window a fresh code is generated and the
    previous one stops working.

    Each number may be sent at most `OTP_MAX_SENDS_PER_HOUR` codes (default 5) per hour;
    beyond that the endpoint returns `429`. The code is only ever delivered by SMS — it is never
    returned in the response or in a cookie.

    `phone` is mandatory and **must be E.164 with a leading `+`**; `channel` defaults to `sms`,
    the only supported value. `destination_masked` is safe to display back to the customer.

    Challenges are stored in MongoDB (hashed), so they survive restarts and are shared between
    replicas.
    """
    phone = req.phone.strip()
    channel = req.channel or "sms"
    if channel != "sms":
        raise HTTPException(status_code=422, detail="unsupported channel")
    if not phone.startswith('+'):
        raise HTTPException(status_code=422, detail="invalid phone format")

    reused = issue_code(phone, channel)

    return {"success": True, "destination_masked": mask_destination(phone), "reused": reused}

@router.post(
    "/verify",
    summary="Check a one-time code",
    response_description="Confirmation that the code was accepted.",
    responses=secured({
        200: json_response(
            "The code was correct and has been consumed.",
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
def verify_otp(req: OtpVerifyIn, response: Response, _: None = Depends(verify_token)):
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

    A code verifies at most once: consumption is atomic, so concurrent or replayed submissions
    of the same code fail with `not_found`.

    Verifying does **not** by itself sign the customer in — follow with
    `POST /customer/mark-verified` to issue the verified-customer cookie.
    """
    phone = req.phone.strip()
    code = req.code.strip()
    channel = req.channel or "sms"
    if channel != "sms":
        raise HTTPException(status_code=422, detail="unsupported channel")

    result = _verify(phone, channel, code)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["reason"])

    # Clear the retired fallback cookie from browsers that still hold one.
    response.delete_cookie(LEGACY_COOKIE_NAME)
    return {"success": True}
