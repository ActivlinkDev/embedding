from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from utils.dependencies import verify_token
from routers.qr.generate_qr_collection import qr_collection

router = APIRouter(tags=["QR"])


def _serialize(doc: dict) -> dict:
    out = dict(doc)
    out["_id"] = str(out.pop("_id"))
    for scan in out.get("scans", []):
        scan.pop("ip_masked", None)
    return out


@router.get("/qr-collection")
def list_qr_collection(
    client_key: str = Query(..., description="Filter by client key (required)"),
    custom_sku: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="unscanned | scanned | paired"),
    batch_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: None = Depends(verify_token),
):
    """List QR codes for a client with optional filters."""
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
