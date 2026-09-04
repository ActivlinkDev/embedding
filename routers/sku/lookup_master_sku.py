from fastapi import APIRouter, Depends, HTTPException, Query

from services.catalog import serialize
from utils.dependencies import verify_token
from .catalog_dependencies import catalog


router = APIRouter(prefix="/sku", tags=["Catalog"])


@router.get("/lookup_master_sku")
def lookup_master_sku(
    id: str = Query(...),
    locale: str = Query(...),
    _: None = Depends(verify_token),
):
    try:
        master, _ = catalog.find_master(master_id=id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not master or locale not in (master.get("locales") or {}):
        raise HTTPException(status_code=404, detail="MasterSKU not found for locale")
    return serialize(master)
