from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from services.catalog import serialize
from utils.dependencies import verify_token
from .catalog_dependencies import catalog


router = APIRouter(prefix="/sku", tags=["Catalog"])


@router.get("/lookup_master_sku_all")
def lookup_master_sku_all(
    id: Optional[str] = Query(None),
    GTIN: Optional[str] = Query(None),
    Make: Optional[str] = Query(None),
    Model: Optional[str] = Query(None),
    _: None = Depends(verify_token),
):
    try:
        master, matched_by = catalog.find_master(
            master_id=id,
            gtin=GTIN,
            make=Make,
            model=Model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not master:
        raise HTTPException(status_code=404, detail="No matching MasterSKU found")
    return {"matchedBy": matched_by, "masterSku": serialize(master)}
