"""Fetch a tenant SKU for administration."""

from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from services.catalog import serialize
from utils.dependencies import verify_token
from .catalog_dependencies import catalog, custom_collection


router = APIRouter(prefix="/sku", tags=["Catalog"])


@router.get("/get_custom_sku")
def get_custom_sku(
    id: str = Query(...),
    clientKey: str = Query(...),
    locale: Optional[str] = Query(None),
    _: None = Depends(verify_token),
):
    client = catalog.client_for_key(clientKey)
    if not client or "Client_ID" not in client:
        raise HTTPException(status_code=404, detail="Invalid clientKey")
    try:
        doc_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")
    custom = custom_collection.find_one({"_id": doc_id, "clientId": client["Client_ID"]})
    if not custom:
        raise HTTPException(status_code=404, detail="CustomSKU not found for client")
    response = {"customSku": serialize(custom)}
    if locale:
        if locale not in (custom.get("enabledLocales") or []):
            raise HTTPException(status_code=404, detail="Locale not enabled on CustomSKU")
        response["resolved"] = serialize(catalog.resolve_custom(custom, locale))
    else:
        response["resolved"] = serialize([
            catalog.resolve_custom(custom, item)
            for item in custom.get("enabledLocales") or []
        ])
    return response
