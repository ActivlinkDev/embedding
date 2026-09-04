"""Search tenant catalogue memberships and their canonical products."""

from typing import Optional
import re

from fastapi import APIRouter, Depends, HTTPException, Query

from services.catalog import serialize
from utils.dependencies import verify_token
from .catalog_dependencies import catalog, custom_collection, master_collection


router = APIRouter(prefix="/sku", tags=["Catalog"])


@router.get("/quick_search")
def quick_search(
    clientKey: str = Query(...),
    q: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    locale: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=500),
    _: None = Depends(verify_token),
):
    client = catalog.client_for_key(clientKey)
    if not client or "Client_ID" not in client:
        raise HTTPException(status_code=404, detail="Invalid clientKey")

    base: dict = {"clientId": client["Client_ID"]}
    if locale:
        base["enabledLocales"] = locale
    search = (q or "").strip()
    if not search and (mode or "").casefold() != "all":
        raise HTTPException(status_code=400, detail="q must be supplied unless mode=all")
    if search and len(search) < 2:
        raise HTTPException(status_code=400, detail="q must be at least two characters")

    query = base
    if search:
        regex = {"$regex": re.escape(search), "$options": "i"}
        master_ids = [
            doc["_id"] for doc in master_collection.find(
                {
                    "$or": [
                        {"identifiers.make": regex},
                        {"identifiers.model": regex},
                        {"identifiers.gtins": regex},
                        {"category": regex},
                    ]
                },
                {"_id": 1},
            ).limit(500)
        ]
        query = {
            "$and": [
                base,
                {"$or": [{"sku": regex}, {"masterSkuId": {"$in": master_ids}}]},
            ]
        }

    customs = list(custom_collection.find(query).sort("skuNormalized", 1).limit(limit))
    results = []
    for custom in customs:
        selected_locale = locale or next(iter(custom.get("enabledLocales") or []), None)
        if not selected_locale:
            continue
        resolved = catalog.resolve_custom(custom, selected_locale)
        if resolved:
            results.append(serialize(resolved))
    return {"count": len(results), "results": results}
