from fastapi import APIRouter, Body, Response, HTTPException, Depends
import os
import time

from utils.dependencies import verify_token

router = APIRouter(tags=["Customer"])

VERIFIED_COOKIE_NAME = os.getenv("VERIFIED_COOKIE_NAME", "verified_customer")
VERIFIED_TTL_SECONDS = int(os.getenv("VERIFIED_TTL_SECONDS", str(24 * 3600)))
COOKIE_SECRET = os.getenv("OTP_COOKIE_SECRET", os.getenv("LOOKUP_API_KEY", "changeme-secret"))

import hmac, hashlib, base64

def _sign(value: str) -> str:
    sig = hmac.new(COOKIE_SECRET.encode(), value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")

def _serialize_verified_cookie(customer_id: str, expiry_ts: int) -> str:
    payload = f"{customer_id}.{expiry_ts}"
    sig = _sign(payload)
    return f"{payload}.{sig}"


@router.post("/customer/mark-verified")
def mark_verified(customer_id: str = Body(...), response: Response = None, _=Depends(verify_token)):
    try:
        expiry = int(time.time()) + VERIFIED_TTL_SECONDS
        cookie_val = _serialize_verified_cookie(customer_id, expiry)
        if response is not None:
            response.set_cookie(
                key=VERIFIED_COOKIE_NAME,
                value=cookie_val,
                httponly=True,
                secure=True,
                samesite="lax",
                max_age=VERIFIED_TTL_SECONDS,
            )
        return {"success": True, "ttl": VERIFIED_TTL_SECONDS}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
