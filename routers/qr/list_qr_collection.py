from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from routers.qr.generate_qr_collection import qr_collection

router = APIRouter(tags=["QR"])


def _serialize(doc: dict) -> dict:
    out = dict(doc)
    out["_id"] = str(out.pop("_id"))
    for scan in out.get("scans", []):
        scan.pop("ip_masked", None)
    return out


@router.get(
    "/qr-collection",
    summary="List a client's QR codes",
    response_description="A page of QR codes plus the total matching the filters.",
    responses=secured({
        200: json_response(
            "The matching codes. `total` is the full count, ignoring `limit` and `offset`.",
            {"total": 50, "limit": 100, "offset": 0, "qr_codes": [{
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
            }]},
        ),
        400: error("`status` is not one of `unscanned`, `scanned`, `paired`.", "status must be one of: unscanned, scanned, paired"),
    }),
)
def list_qr_collection(
    client_key: str = Query(..., description="**Mandatory.** Only this client's codes are returned.", examples=["acme_uk_live"]),
    custom_sku: Optional[str] = Query(None, description="Only codes bound to this CustomSKU.", examples=["681aa2f1c4b21d0f8c9e0012"]),
    status: Optional[str] = Query(
        None,
        description="Only codes in this state. Must be `unscanned`, `scanned` or `paired`; anything else returns `400`.",
        examples=["paired"],
    ),
    batch_id: Optional[str] = Query(
        None,
        description="Only codes from one generation batch, as returned by `POST /qr-collection/generate`.",
        examples=["0f2c4d1e-8a7b-4c5d-9e3f-2a1b0c9d8e7f"],
    ),
    limit: int = Query(100, ge=1, le=500, description="Page size, between 1 and 500.", examples=[100]),
    offset: int = Query(0, ge=0, description="How many codes to skip. Combine with `limit` to page through results.", examples=[0]),
    _: None = Depends(verify_token),
):
    """
    List a client's QR codes, with optional filters and paging.

    `client_key` is mandatory and scopes every result. Narrow further by `custom_sku`,
    `status` or `batch_id` — the last is how you retrieve a batch you generated earlier.

    Results are **paged**: `limit` and `offset` control the window, sorted by creation time so
    paging is stable, while `total` reports the full count matching the filters. Raw IPs are
    stripped from every scan history.

    Each entry is the **full** QR document, including its base64 image, so responses get large
    quickly — keep `limit` modest when you only need the keys.
    """
    query: dict = {"client_key": client_key}
    if custom_sku is not None:
        query["custom_sku"] = custom_sku
    if status is not None:
        if status not in ("unscanned", "scanned", "paired"):
            raise HTTPException(status_code=400, detail="status must be one of: unscanned, scanned, paired")
        query["status"] = status
    if batch_id is not None:
        query["batch_id"] = batch_id

    total = qr_collection.count_documents(query)
    # Fix 10: stable sort so skip/limit pagination returns consistent results
    docs = list(qr_collection.find(query).sort("created_at", 1).skip(offset).limit(limit))
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "qr_codes": [_serialize(d) for d in docs],
    }
