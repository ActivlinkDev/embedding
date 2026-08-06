from fastapi import APIRouter, Body, Response, HTTPException, Depends
import os
import time

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

router = APIRouter(tags=["Customers"])

VERIFIED_COOKIE_NAME = os.getenv("VERIFIED_COOKIE_NAME", "verified_customer")
VERIFIED_TTL_SECONDS = int(os.getenv("VERIFIED_TTL_SECONDS", str(24 * 3600)))
COOKIE_SECRET = os.getenv("OTP_COOKIE_SECRET") or os.getenv("LOOKUP_API_KEY")
if not COOKIE_SECRET or COOKIE_SECRET == "changeme-secret":
    raise RuntimeError("OTP_COOKIE_SECRET or LOOKUP_API_KEY must be set to a strong secret")

import hmac, hashlib, base64

def _sign(value: str) -> str:
    sig = hmac.new(COOKIE_SECRET.encode(), value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")

def _serialize_verified_cookie(customer_id: str, expiry_ts: int) -> str:
    payload = f"{customer_id}.{expiry_ts}"
    sig = _sign(payload)
    return f"{payload}.{sig}"


@router.post(
    "/customer/mark-verified",
    summary="Issue a signed cookie marking a customer as verified",
    response_description="Confirmation and the cookie's lifetime in seconds.",
    responses=secured({
        200: json_response("The cookie was issued.", {"success": True, "ttl": 86400}),
        500: error("The cookie could not be generated.", "..."),
    }),
)
def mark_verified(
    customer_id: str = Body(
        ...,
        description="**Mandatory.** The customer to mark as verified. Written into the signed cookie.",
        examples=["6820f1c9a4b21d0f8c9e9001"],
    ),
    response: Response = None,
    _=Depends(verify_token),
):
    """
    Final step of customer sign-in: issue the session cookie that marks this browser as a
    verified customer.

    Call it **after** `POST /otp/verify` has confirmed the code. The response sets an
    HTTP-only, `Secure`, `SameSite=Lax` cookie holding the customer id, an expiry, and an HMAC
    signature — so it cannot be forged or pointed at another customer without the server secret.
    `ttl` is its lifetime in seconds (24 hours by default, via `VERIFIED_TTL_SECONDS`).

    **This endpoint does not verify anything itself.** It trusts that the OTP step already
    passed and issues a cookie for whatever `customer_id` it is given, so never expose it
    directly to a browser — call it server-side once the code has been checked.

    `customer_id` is mandatory.
    """
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
