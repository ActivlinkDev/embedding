import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from routers.qr.generate_qr_collection import qr_collection, clientkey_collection
from utils.ip_geolocation import get_client_ip, lookup_country, country_code_to_locale, _mask_ip

from utils.api_docs import error

router = APIRouter(tags=["QR"])

FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "").rstrip("/")
_HEX_RE = re.compile(r"^[0-9A-F]{5}$")


@router.get(
    "/qr/scan/{hex_key}",
    summary="Public QR scan target (redirects the customer)",
    response_description="A 302 redirect to the right frontend page for this code.",
    responses={
        302: {"description": "Redirect to the registration or product page, with the code's context in the query string."},
        404: error(
            "The key is not five hex characters, or no such code exists. The two cases are "
            "deliberately indistinguishable.",
            "QR code not found",
        ),
    },
)
def scan_qr(hex_key: str, request: Request):
    """
    The URL encoded in every QR code. **Customers' browsers land here — you do not call it.**

    It records the scan, works out where the customer should go, and issues a **302 redirect**.
    There is no JSON response.

    **No authentication**, by necessity: anyone scanning a printed code reaches it. The key is a
    five-character hex string, matched case-insensitively; anything else is `404`.

    Each scan appends an event holding the country resolved from the IP (the IP itself is stored
    masked and never returned), the user agent and a timestamp, increments `scan_count`, and
    moves the code to `scanned` — or `paired` if a device is already bound.

    **Where it redirects**, in priority order:

    1. Code paired to a device → the device page for that registration
    2. Code bound to a `custom_sku` → the product page, pre-filled with any stored identifiers
    3. Stored `make`+`model`, or a `gtin` → the product page, pre-filled
    4. Otherwise → a generic start page carrying the code

    The locale in the redirect comes from the scanner's country, and the destination host from
    the client's `redirect_base_url` where configured.
    """
    hex_key = hex_key.upper()
    if not _HEX_RE.match(hex_key):
        raise HTTPException(status_code=404, detail="QR code not found")

    # Fix 8: projection excludes qr_image_b64 (large field not needed for scan redirect)
    doc = qr_collection.find_one(
        {"hex_key": hex_key},
        {"qr_image_b64": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="QR code not found")

    ip = get_client_ip(request)
    geo = lookup_country(ip)
    locale = country_code_to_locale(geo["country_code"])

    scan_event = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "ip_masked": _mask_ip(ip),  # Fix 9: handles both IPv4 and IPv6
        "country_code": geo["country_code"],
        "country_name": geo["country_name"],
        "resolved_locale": locale,
        "user_agent": request.headers.get("user-agent", ""),
    }

    new_status = "paired" if doc.get("device_id") else "scanned"
    qr_collection.update_one(
        {"hex_key": hex_key},
        {
            "$push": {"scans": scan_event},
            "$inc": {"scan_count": 1},
            "$set": {"status": new_status},
        },
    )

    client_key = doc.get("client_key", "")
    client_doc = clientkey_collection.find_one({"ClientKey": client_key}) or {}
    base_url = (client_doc.get("redirect_base_url") or FRONTEND_BASE_URL).rstrip("/")

    device_id = doc.get("device_id")
    custom_sku = doc.get("custom_sku")
    device_params = doc.get("device_params") or {}

    if device_id:
        params = {"id": device_id, "clientKey": client_key, "locale": locale}
        redirect_url = f"{base_url}/device?{urlencode(params)}"

    elif custom_sku:
        params = {"clientKey": client_key, "locale": locale, "id": custom_sku}
        if device_params.get("serial"):
            params["serial"] = device_params["serial"]
        if device_params.get("make"):
            params["make"] = device_params["make"]
        if device_params.get("model"):
            params["model"] = device_params["model"]
        if device_params.get("gtin"):
            params["gtin"] = device_params["gtin"]
        redirect_url = f"{base_url}/product?{urlencode(params)}"

    elif device_params.get("make") and device_params.get("model"):
        params = {"clientKey": client_key, "locale": locale}
        params["make"] = device_params["make"]
        params["model"] = device_params["model"]
        if device_params.get("serial"):
            params["serial"] = device_params["serial"]
        if device_params.get("gtin"):
            params["gtin"] = device_params["gtin"]
        redirect_url = f"{base_url}/product?{urlencode(params)}"

    elif device_params.get("gtin"):
        params = {"clientKey": client_key, "locale": locale, "gtin": device_params["gtin"]}
        if device_params.get("serial"):
            params["serial"] = device_params["serial"]
        redirect_url = f"{base_url}/product?{urlencode(params)}"

    else:
        params = {"qr": hex_key, "locale": locale, "clientKey": client_key}
        redirect_url = f"{base_url}/start?{urlencode(params)}"

    return RedirectResponse(url=redirect_url, status_code=302)
