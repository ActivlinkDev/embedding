from fastapi import APIRouter, Body, HTTPException, Depends, Response
from pymongo import MongoClient
from bson import ObjectId
import os

from utils.dependencies import verify_token

router = APIRouter(tags=["Customer"])

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI not set in environment")

client = MongoClient(MONGO_URI)
db = client["Activlink"]
customer_collection = db["Customer"]


def _digits_only(s: str) -> str:
    if not s:
        return ""
    return "".join(ch for ch in s if ch.isdigit() or ch == '+')


@router.post("/customer/authenticate")
def authenticate_customer(
    customer_id: str = Body(...),
    phone: str = Body(...),
    response: Response = None,
    _=Depends(verify_token),
):
    """Authenticate a customer by customer_id (Mongo _id) and phone number.

    Request body: { "customer_id": "<hexid>", "phone": "+441234..." }

    Responses:
      - { authenticated: true } when phone matches
      - { authenticated: false, reason: "..." } when not matched or missing

    Note: this endpoint never returns the full customer document.
    """
    try:
        try:
            objid = ObjectId(customer_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid customer_id")

        doc = customer_collection.find_one({"_id": objid})
        if not doc:
            raise HTTPException(status_code=404, detail="Customer not found")

        # Try common fields for phone
        stored_phone = doc.get("telephone") or doc.get("phone") or doc.get("mobile") or ""

        if not stored_phone:
            return {"authenticated": False, "reason": "no phone on record"}

        # Prefer using phonenumbers library if available for robust comparison
        try:
            import phonenumbers

            def _format(num: str):
                if not num:
                    return ""
                try:
                    # parse with None region; phonenumbers will accept E.164 or infer
                    p = phonenumbers.parse(num, None)
                    if phonenumbers.is_valid_number(p):
                        return phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
                    # fallback to digits-only
                    return _digits_only(num)
                except Exception:
                    return _digits_only(num)

            a = _format(stored_phone)
            b = _format(phone)
            matched = bool(a and b and a == b)
        except Exception:
            # phonenumbers not available or failed; fall back to digit-only comparison
            a = _digits_only(stored_phone)
            b = _digits_only(phone)
            matched = bool(a and b and a == b)

        if matched:
            # Trigger OTP request flow: reuse OTP module helpers so we don't duplicate logic
            try:
                # Import internal helpers from otp module
                from routers.otp import _get_record, _generate_code, _save_code, _send_sms, serialize_cookie, mask_destination, RESEND_COOLDOWN_SECONDS, _now_ms

                channel = "sms"
                phone_norm = phone.strip()

                existing = _get_record(phone_norm, channel)
                now = _now_ms()
                reused = False
                if existing and (now - existing["createdAt"]) < RESEND_COOLDOWN_SECONDS * 1000:
                    code = existing["code"]
                    reused = True
                else:
                    code = _generate_code()
                    _save_code(phone_norm, channel, code)

                if not reused:
                    # send SMS (may raise HTTPException which will propagate)
                    _send_sms(phone_norm, f"Your validation code is: {code}")

                # set cookie fallback similar to OTP.request
                try:
                    cookie_val = serialize_cookie(code, phone_norm)
                    if response is not None:
                        response.set_cookie(
                            key="otp_fallback",
                            value=cookie_val,
                            httponly=True,
                            secure=True,
                            samesite="lax",
                            max_age=5 * 60,
                        )
                except Exception:
                    # ignore cookie failures
                    pass

                return {"authenticated": True, "otp_sent": True, "destination_masked": mask_destination(phone_norm), "reused": reused}
            except HTTPException:
                # propagate OTP SMS-send errors
                raise
            except Exception:
                # If OTP subsystem fails, still return authenticated true but indicate otp failure
                return {"authenticated": True, "otp_sent": False, "reason": "otp_failed"}
        else:
            return {"authenticated": False, "reason": "phone mismatch"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {type(e).__name__}: {e}")
