from fastapi import APIRouter, HTTPException, Depends

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


@router.get("/qr/{hex_key}")
def get_qr(hex_key: str, _: None = Depends(verify_token)):
    """Retrieve a QR code document by its 5-character hex key."""
    doc = qr_collection.find_one({"hex_key": hex_key.upper()})
    if not doc:
        raise HTTPException(status_code=404, detail="QR code not found")
    return _serialize(doc)
