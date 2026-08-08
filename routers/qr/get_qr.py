from fastapi import APIRouter, HTTPException, Depends

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from routers.qr.generate_qr_collection import qr_collection

router = APIRouter(tags=["QR"])


def _serialize(doc: dict) -> dict:
    out = dict(doc)
    out["_id"] = str(out.pop("_id"))
    # Strip raw IPs from scan history
    for scan in out.get("scans", []):
        scan.pop("ip_masked", None)
    return out


@router.get(
    "/qr/{hex_key}",
    summary="Fetch a QR code and its scan history",
    response_description="The QR document, including scan history with IPs removed.",
    responses=secured({
        200: json_response("The code was found.", {
                "_id": "68f0a1b2c3d4e5f6a7b80011",
                "hex_key": "3F9A1",
                "batch_id": "0f2c4d1e-8a7b-4c5d-9e3f-2a1b0c9d8e7f",
                "client_key": "acme_uk_live",
                "custom_sku": "681aa2f1c4b21d0f8c9e0012",
                "device_params": {"serial": "SN-8841203", "make": "Bosch", "model": "SMS6ZCI00G", "gtin": None},
                "scan_url": "https://api.activlink.io/qr/scan/3F9A1",
                "status": "paired",
                "scan_count": 2,
                "scans": [
                    {
                        "scanned_at": "2026-08-06T10:14:52.113000+00:00",
                        "country_code": "GB",
                        "country_name": "United Kingdom",
                        "resolved_locale": "en_GB",
                        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",
                    }
                ],
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "paired_at": "2026-08-06T10:20:00+00:00",
                "created_at": "2026-08-01T09:00:00+00:00",
                "created_by": "jane.okafor",
            }),
        404: error("No QR code with this hex key.", "QR code not found"),
    }),
)
def get_qr(hex_key: str, _: None = Depends(verify_token)):
    """
    Fetch one QR code by its five-character hex key, including its full scan history.

    The key is matched case-insensitively. Each entry in `scans` carries the timestamp, the
    country resolved from the scanner's IP, and the user agent — **the IP is stripped from the
    response**, even though a masked copy is stored.

    `status` is `unscanned`, `scanned` or `paired`; `device_id` and `paired_at` are present only
    once the code has been paired.

    The response includes `qr_image_b64`, the printable PNG, which makes it large. Use
    `GET /qr-collection` to list codes without pulling each image individually.
    """
    doc = qr_collection.find_one({"hex_key": hex_key.upper()})
    if not doc:
        raise HTTPException(status_code=404, detail="QR code not found")
    return _serialize(doc)
