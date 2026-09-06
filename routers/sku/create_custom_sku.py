"""Create tenant catalogue memberships with explicit overrides only."""

from __future__ import annotations

from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from services.catalog import SCHEMA_VERSION, normalize_sku, serialize, utc_now
from utils.dependencies import verify_token
from .catalog_dependencies import catalog, custom_collection, locale_collection, master_collection
from .category_validation import validate_category
from .create_master_sku import MasterSKURequest, create_master_sku_service


router = APIRouter(prefix="/sku", tags=["Catalog"])


class CustomLink(BaseModel):
    Type: Optional[str] = None
    URL: Optional[str] = None


class LocaleDetails(BaseModel):
    Title: Optional[str] = None
    Price: Optional[float] = None
    Currency: Optional[str] = None
    Category: Optional[str] = None
    GTL: Optional[int] = None
    GTP: Optional[int] = None
    Promo_Code: Optional[str] = None
    Custom_Links: Optional[List[CustomLink]] = None
    Generate_Offers: Optional[bool] = None


class CustomSKURequest(BaseModel):
    ClientKey: str
    Locale: str
    SKU: str
    Source: str
    masterSkuId: Optional[str] = None
    GTIN: Optional[str] = ""
    Make: Optional[str] = ""
    Model: Optional[str] = ""
    Category: Optional[str] = None
    Locale_Details: Optional[LocaleDetails] = None
    Global_Promotion: Optional[str] = None
    add_pricing: Optional[bool] = True


def _locale_override(details: Optional[LocaleDetails]) -> dict:
    if details is None:
        return {}
    supplied = details.model_fields_set
    result = {}
    mapping = {
        "Title": "title",
        "Price": "price",
        "Currency": "currency",
        "Category": "category",
        "Promo_Code": "promotion",
        "Generate_Offers": "generateOffers",
    }
    for source, target in mapping.items():
        if source in supplied:
            value = getattr(details, source)
            if source == "Category":
                value = validate_category(value, "Locale_Details.Category")
            result[target] = value
    if "Custom_Links" in supplied:
        result["customLinks"] = (
            None
            if details.Custom_Links is None
            else [item.model_dump(exclude_none=True) for item in details.Custom_Links]
        )
    guarantee = {}
    if "GTL" in supplied:
        guarantee["labourMonths"] = details.GTL
    if "GTP" in supplied:
        guarantee["partsMonths"] = details.GTP
    if guarantee:
        result["guarantee"] = guarantee
    return result


def _master_for_request(
    data: CustomSKURequest,
    background_tasks: BackgroundTasks,
    request: Optional[Request],
) -> dict:
    if data.masterSkuId:
        try:
            master = master_collection.find_one({"_id": ObjectId(data.masterSkuId)})
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid masterSkuId")
        if not master:
            raise HTTPException(status_code=404, detail="MasterSKU not found")
        if data.Locale not in (master.get("locales") or {}):
            raise HTTPException(status_code=409, detail=f"MasterSKU has no {data.Locale} locale")
        return master

    response = create_master_sku_service(
        MasterSKURequest(
            Make=data.Make or "",
            Model=data.Model or "",
            GTIN=data.GTIN or "",
            locale=data.Locale,
            Category=data.Category,
        ),
        background_tasks,
        request,
        bool(data.add_pricing),
    )
    master_data = response.get("masterSku") or {}
    try:
        return master_collection.find_one({"_id": ObjectId(master_data["_id"])})
    except Exception:
        raise HTTPException(status_code=500, detail="MasterSKU could not be resolved after creation")


def create_custom_sku_service(
    data: CustomSKURequest,
    background_tasks: BackgroundTasks,
    request: Optional[Request] = None,
) -> dict:
    client = catalog.client_for_key(data.ClientKey)
    if not client or "Client_ID" not in client:
        raise HTTPException(status_code=404, detail="Invalid clientKey")
    if not locale_collection.find_one({"locale": data.Locale}, {"_id": 1}):
        raise HTTPException(status_code=404, detail=f"Locale {data.Locale} not found")
    if "." in data.Locale or data.Locale.startswith("$"):
        raise HTTPException(status_code=400, detail="Invalid locale key")

    sku = data.SKU.strip()
    if not sku:
        raise HTTPException(status_code=400, detail="SKU cannot be empty")
    source = data.Source.strip()
    if not source:
        raise HTTPException(status_code=400, detail="Source cannot be empty")

    client_id = client["Client_ID"]
    existing = custom_collection.find_one({
        "clientId": client_id,
        "skuNormalized": normalize_sku(sku),
    })
    provided = data.model_fields_set
    # Validated before anything is written: `_master_for_request` can create a
    # MasterSKU, and a category rejected after that would strand it.
    locale_override = _locale_override(data.Locale_Details)
    root_category = validate_category(data.Category) if "Category" in provided else None

    master = None
    if data.masterSkuId:
        master = _master_for_request(data, background_tasks, request)
    elif existing and isinstance(existing.get("masterSkuId"), ObjectId):
        master = master_collection.find_one({"_id": existing["masterSkuId"]})
        if master and data.Locale not in (master.get("locales") or {}):
            master = None
    if master is None:
        master = _master_for_request(data, background_tasks, request)
    now = utc_now()
    locale_was_enabled = bool(existing and data.Locale in (existing.get("enabledLocales") or []))

    if existing:
        if existing.get("masterSkuId") != master["_id"]:
            raise HTTPException(status_code=409, detail="SKU already belongs to another MasterSKU")
        set_ops = {"updatedAt": now}
        for key, value in locale_override.items():
            if key == "guarantee" and isinstance(value, dict):
                for guarantee_key, guarantee_value in value.items():
                    set_ops[
                        f"overrides.locales.{data.Locale}.guarantee.{guarantee_key}"
                    ] = guarantee_value
            else:
                set_ops[f"overrides.locales.{data.Locale}.{key}"] = value
        if "Category" in provided:
            set_ops["overrides.category"] = root_category
        if "Global_Promotion" in provided:
            set_ops["overrides.globalPromotion"] = data.Global_Promotion
        custom_collection.update_one(
            {"_id": existing["_id"]},
            {
                "$set": set_ops,
                "$addToSet": {"enabledLocales": data.Locale, "sources": source},
            },
        )
        saved = custom_collection.find_one({"_id": existing["_id"]})
        message = "CustomSKU updated"
    else:
        overrides: dict = {"locales": {}}
        if locale_override:
            overrides["locales"][data.Locale] = locale_override
        if "Category" in provided:
            overrides["category"] = root_category
        if "Global_Promotion" in provided:
            overrides["globalPromotion"] = data.Global_Promotion
        doc = {
            "schemaVersion": SCHEMA_VERSION,
            "clientId": client_id,
            "masterSkuId": master["_id"],
            "sku": sku,
            "skuNormalized": normalize_sku(sku),
            "enabledLocales": [data.Locale],
            "sources": [source],
            "overrides": overrides,
            "createdAt": now,
            "updatedAt": now,
        }
        try:
            result = custom_collection.insert_one(doc)
        except DuplicateKeyError:
            raise HTTPException(status_code=409, detail="SKU already exists for this client")
        saved = custom_collection.find_one({"_id": result.inserted_id})
        message = "CustomSKU created"

    resolved = catalog.resolve_custom(saved, data.Locale)
    try:
        from routers.widget_quote import warm_widget_cache
        background_tasks.add_task(warm_widget_cache, data.ClientKey, str(saved["_id"]), data.Locale)
    except Exception:
        pass
    return {
        "message": message,
        "created": existing is None,
        "localeAdded": bool(existing and not locale_was_enabled),
        "localeAlreadyEnabled": locale_was_enabled,
        "customSku": serialize(saved),
        "resolved": serialize(resolved),
    }


@router.post("/create_custom_sku")
def create_custom_sku(
    data: CustomSKURequest,
    background_tasks: BackgroundTasks,
    request: Request,
    _: None = Depends(verify_token),
):
    return create_custom_sku_service(data, background_tasks, request)
