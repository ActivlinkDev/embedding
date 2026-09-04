"""Resolve one tenant product by client or canonical identifiers."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from services.catalog import serialize
from utils.dependencies import verify_token
from .catalog_dependencies import catalog


router = APIRouter(prefix="/sku", tags=["Catalog"])
@router.get("/lookup_custom_sku")
def lookup_sku(
    clientKey: str = Query(...),
    locale: str = Query(...),
    Make: Optional[str] = Query(None),
    Model: Optional[str] = Query(None),
    GTIN: Optional[str] = Query(None),
    SKU: Optional[str] = Query(None),
    id: Optional[str] = Query(None),
    _: None = Depends(verify_token),
):
    if not (id or SKU or GTIN or (Make and Model)):
        raise HTTPException(status_code=400, detail="Provide id, SKU, GTIN, or Make and Model")
    try:
        resolved, matched_by = catalog.resolve_lookup(
            client_key=clientKey,
            locale=locale,
            custom_id=id,
            sku=SKU,
            gtin=GTIN,
            make=Make,
            model=Model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not resolved:
        raise HTTPException(status_code=404, detail="No matching SKU found")
    return {"matchedBy": matched_by, "count": 1, "results": [serialize(resolved)]}
