"""Patch a tenant SKU or its explicit overrides."""

from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from services.catalog import normalize_sku, serialize, utc_now
from utils.dependencies import verify_token
from .catalog_dependencies import catalog, custom_collection


router = APIRouter(prefix="/sku", tags=["Catalog"])


class LocaleDetailsPatch(BaseModel):
    Title: Optional[str] = None
    Price: Optional[float] = None
    Currency: Optional[str] = None
    Category: Optional[str] = None
    GTL: Optional[int] = None
    GTP: Optional[int] = None
    Promo_Code: Optional[str] = None
    Custom_Links: Optional[List[dict]] = None
    Generate_Offers: Optional[bool] = None


class UpdateCustomSKURequest(BaseModel):
    ClientKey: str
    id: str
    SKU: Optional[str] = None
    Category: Optional[str] = None
    Global_Promotion: Optional[str] = None
    Locale: Optional[str] = None
    Locale_Details: Optional[LocaleDetailsPatch] = None
    Inherit: List[str] = Field(default_factory=list)


LOCALE_FIELDS = {
    "Title": "title",
    "Price": "price",
    "Currency": "currency",
    "Category": "category",
    "Promo_Code": "promotion",
    "Custom_Links": "customLinks",
    "Generate_Offers": "generateOffers",
}


def _inherit_path(name: str, locale: Optional[str]) -> str:
    root = {
        "Category": "overrides.category",
        "Global_Promotion": "overrides.globalPromotion",
        "globalPromotion": "overrides.globalPromotion",
    }
    if name in root:
        return root[name]
    if not locale:
        raise HTTPException(status_code=400, detail=f"Locale is required to inherit {name}")
    locale_names = {
        **LOCALE_FIELDS,
        "GTL": "guarantee.labourMonths",
        "GTP": "guarantee.partsMonths",
        "Locale_Category": "category",
        "localeCategory": "category",
        "category": "category",
        "title": "title",
        "price": "price",
        "currency": "currency",
        "promotion": "promotion",
        "customLinks": "customLinks",
        "generateOffers": "generateOffers",
        "partsMonths": "guarantee.partsMonths",
        "labourMonths": "guarantee.labourMonths",
    }
    if name not in locale_names:
        raise HTTPException(status_code=400, detail=f"Unknown inherited field: {name}")
    return f"overrides.locales.{locale}.{locale_names[name]}"


@router.post("/update_custom_sku")
def update_custom_sku(
    data: UpdateCustomSKURequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_token),
):
    client = catalog.client_for_key(data.ClientKey)
    if not client or "Client_ID" not in client:
        raise HTTPException(status_code=404, detail="Invalid clientKey")
    try:
        doc_id = ObjectId(data.id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")

    existing = custom_collection.find_one({"_id": doc_id, "clientId": client["Client_ID"]})
    if not existing:
        raise HTTPException(status_code=404, detail="CustomSKU not found for client")
    if data.Locale and data.Locale not in (existing.get("enabledLocales") or []):
        raise HTTPException(status_code=404, detail=f"Locale {data.Locale} not enabled on CustomSKU")
    if data.Locale and ("." in data.Locale or data.Locale.startswith("$")):
        raise HTTPException(status_code=400, detail="Invalid locale key")
    if data.Locale_Details is not None and not data.Locale:
        raise HTTPException(status_code=400, detail="Locale is required with Locale_Details")

    set_ops = {"updatedAt": utc_now()}
    unset_ops = {}
    provided = data.model_fields_set

    if "SKU" in provided:
        sku = (data.SKU or "").strip()
        if not sku:
            raise HTTPException(status_code=400, detail="SKU cannot be empty")
        set_ops.update({"sku": sku, "skuNormalized": normalize_sku(sku)})
    if "Category" in provided:
        set_ops["overrides.category"] = data.Category
    if "Global_Promotion" in provided:
        set_ops["overrides.globalPromotion"] = data.Global_Promotion

    if data.Locale_Details is not None:
        locale_provided = data.Locale_Details.model_fields_set
        for source, target in LOCALE_FIELDS.items():
            if source in locale_provided:
                set_ops[f"overrides.locales.{data.Locale}.{target}"] = getattr(data.Locale_Details, source)
        if "GTL" in locale_provided:
            set_ops[f"overrides.locales.{data.Locale}.guarantee.labourMonths"] = data.Locale_Details.GTL
        if "GTP" in locale_provided:
            set_ops[f"overrides.locales.{data.Locale}.guarantee.partsMonths"] = data.Locale_Details.GTP

    for field in data.Inherit:
        path = _inherit_path(field, data.Locale)
        unset_ops[path] = ""
        # An explicit inherit instruction wins if this request also supplies
        # a value for the same path; MongoDB rejects $set/$unset overlap.
        set_ops.pop(path, None)

    if set(set_ops) == {"updatedAt"} and not unset_ops:
        raise HTTPException(status_code=400, detail="No updatable fields provided")
    update = {"$set": set_ops}
    if unset_ops:
        update["$unset"] = unset_ops
    try:
        custom_collection.update_one({"_id": doc_id}, update)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="SKU already exists for this client")

    saved = custom_collection.find_one({"_id": doc_id})
    locales = [data.Locale] if data.Locale else list(saved.get("enabledLocales") or [])
    resolved = [catalog.resolve_custom(saved, locale) for locale in locales]

    try:
        from routers.widget_quote import warm_widget_cache
        for locale in locales:
            background_tasks.add_task(warm_widget_cache, data.ClientKey, data.id, locale)
    except Exception:
        pass
    return {
        "message": "CustomSKU updated",
        "customSku": serialize(saved),
        "resolved": serialize(resolved[0] if len(resolved) == 1 else resolved),
    }
