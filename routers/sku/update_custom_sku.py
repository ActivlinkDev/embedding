from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import os
from dotenv import load_dotenv

from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Catalog"]
)

mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    raise RuntimeError("MONGO_URI not set in environment.")

client = MongoClient(mongo_uri)
db = client["Activlink"]
customsku_collection = db["CustomSKU"]
clientkey_collection = db["ClientKey"]


class LocaleDetailsPatch(BaseModel):
    Title: Optional[str] = None
    Price: Optional[float] = None
    GTL: Optional[int] = None
    GTP: Optional[int] = None
    Promo_Code: Optional[str] = None


class UpdateCustomSKURequest(BaseModel):
    ClientKey: str
    id: str = Field(..., description="CustomSKU document id")
    SKU: Optional[str] = None
    Category: Optional[str] = None
    Global_Promotion: Optional[str] = None
    Locale: Optional[str] = None
    Locale_Details: Optional[LocaleDetailsPatch] = None


def _to_id_str(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return doc
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


@router.post("/update_custom_sku")
def update_custom_sku(data: UpdateCustomSKURequest, background_tasks: BackgroundTasks, _: None = Depends(verify_token)):
    clientkey_doc = clientkey_collection.find_one({"ClientKey": data.ClientKey})
    if not clientkey_doc or "Client_ID" not in clientkey_doc:
        raise HTTPException(status_code=404, detail="Invalid clientKey")
    client_id = clientkey_doc["Client_ID"]

    try:
        doc_id = ObjectId(data.id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")

    existing = customsku_collection.find_one({"_id": doc_id, "Client": client_id})
    if not existing:
        raise HTTPException(status_code=404, detail="CustomSKU not found for client")

    if data.Locale_Details and not data.Locale:
        raise HTTPException(status_code=400, detail="Locale is required when Locale_Details is provided")

    set_ops = {}
    unset_ops = {}
    update_kwargs = {}

    if data.SKU is not None:
        new_sku = data.SKU.strip()
        if not new_sku:
            raise HTTPException(status_code=400, detail="SKU cannot be empty")

        dupe_query = {
            "Client": client_id,
            "Identifiers.SKU": new_sku,
            "_id": {"$ne": doc_id},
        }
        duplicate = customsku_collection.find_one(dupe_query, {"_id": 1})
        if duplicate:
            raise HTTPException(status_code=409, detail="SKU already exists for this client")

        set_ops["Identifiers.SKU"] = new_sku

    if data.Category is not None:
        set_ops["Category"] = data.Category

    if data.Global_Promotion is not None:
        set_ops["Global_Promotion"] = data.Global_Promotion

    if data.Locale:
        locale_exists = any(
            isinstance(entry, dict) and entry.get("locale") == data.Locale
            for entry in (existing.get("Locale_Specific_Data") or [])
        )
        if not locale_exists:
            raise HTTPException(status_code=404, detail=f"Locale {data.Locale} not found on CustomSKU")

    if data.Locale_Details:
        locale_set_ops = {}
        locale_unset_ops = {}

        # A field that is explicitly present in the request but null is treated
        # as a clear ($unset). A field that is simply omitted is left untouched.
        # `model_fields_set` lets us tell those two cases apart (pydantic v2).
        provided = data.Locale_Details.model_fields_set
        field_paths = {
            "Title": "Locale_Specific_Data.$[loc].Title",
            "Price": "Locale_Specific_Data.$[loc].MSRP",
            "GTL": "Locale_Specific_Data.$[loc].Guarantees.Labour",
            "GTP": "Locale_Specific_Data.$[loc].Guarantees.Parts",
            "Promo_Code": "Locale_Specific_Data.$[loc].Guarantees.Promotion",
        }
        for field, path in field_paths.items():
            if field not in provided:
                continue
            value = getattr(data.Locale_Details, field)
            if value is None:
                locale_unset_ops[path] = ""
            else:
                locale_set_ops[path] = value

        if locale_set_ops or locale_unset_ops:
            set_ops.update(locale_set_ops)
            unset_ops.update(locale_unset_ops)
            update_kwargs["array_filters"] = [{"loc.locale": data.Locale}]

    if not set_ops and not unset_ops:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    update_doc = {}
    if set_ops:
        update_doc["$set"] = set_ops
    if unset_ops:
        update_doc["$unset"] = unset_ops

    customsku_collection.update_one(
        {"_id": doc_id, "Client": client_id},
        update_doc,
        **update_kwargs
    )

    updated = customsku_collection.find_one({"_id": doc_id, "Client": client_id})
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to load updated CustomSKU")

    # Refresh the embeddable-widget quote cache for any affected locale(s), since
    # MSRP / guarantees / category changes alter pricing.
    from routers.widget_quote import warm_widget_cache
    if data.Locale:
        affected_locales = [data.Locale]
    else:
        affected_locales = [
            entry.get("locale")
            for entry in (updated.get("Locale_Specific_Data") or [])
            if isinstance(entry, dict) and entry.get("locale")
        ]
    for loc in affected_locales:
        background_tasks.add_task(warm_widget_cache, data.ClientKey, str(doc_id), loc)

    return {
        "message": "CustomSKU updated",
        "customsku": _to_id_str(updated),
    }
