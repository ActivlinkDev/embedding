import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from routers.qr.generate_qr_collection import qr_collection
from utils.ip_geolocation import get_client_ip, lookup_country, country_code_to_locale, _mask_ip

router = APIRouter(tags=["QR"])

FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "").rstrip("/")
_HEX_RE = re.compile(r"^[0-9A-F]{5}$")


@router.get("/qr/scan/{hex_key}")
def scan_qr(hex_key: str, request: Request):
    """Public endpoint encoded in QR codes. Detects country from IP,
    records the scan event, and redirects to the appropriate frontend page."""
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
    device_id = doc.get("device_id")
    custom_sku = doc.get("custom_sku")
    device_params = doc.get("device_params") or {}

    if device_id:
        params = {"id": device_id, "clientKey": client_key, "locale": locale}
        redirect_url = f"{FRONTEND_BASE_URL}/device?{urlencode(params)}"

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
        redirect_url = f"{FRONTEND_BASE_URL}/product?{urlencode(params)}"

    elif device_params.get("make") and device_params.get("model"):
        params = {"clientKey": client_key, "locale": locale}
        params["make"] = device_params["make"]
        params["model"] = device_params["model"]
        if device_params.get("serial"):
            params["serial"] = device_params["serial"]
        if device_params.get("gtin"):
            params["gtin"] = device_params["gtin"]
        redirect_url = f"{FRONTEND_BASE_URL}/product?{urlencode(params)}"

    elif device_params.get("gtin"):
        params = {"clientKey": client_key, "locale": locale, "gtin": device_params["gtin"]}
        if device_params.get("serial"):
            params["serial"] = device_params["serial"]
        redirect_url = f"{FRONTEND_BASE_URL}/product?{urlencode(params)}"

    else:
        params = {"qr": hex_key, "locale": locale, "clientKey": client_key}
        redirect_url = f"{FRONTEND_BASE_URL}/start?{urlencode(params)}"

    return RedirectResponse(url=redirect_url, status_code=302)
