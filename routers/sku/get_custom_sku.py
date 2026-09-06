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
        locales = [locale]
    else:
        locales = list(custom.get("enabledLocales") or [])

    resolved = [catalog.resolve_custom(custom, item) for item in locales]
    # The baseline each locale falls back to once its overrides are removed.
    # An editor cannot derive this from `resolved`, where an override has
    # already replaced the inherited value.
    inherited = [
        catalog.resolve_custom(custom, item, ignore_overrides=True)
        for item in locales
    ]

    if locale:
        response["resolved"] = serialize(resolved[0])
        response["inherited"] = serialize(inherited[0])
    else:
        response["resolved"] = serialize(resolved)
        response["inherited"] = serialize(inherited)
    return response
