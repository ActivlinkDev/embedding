# create_custom_sku.py

from fastapi import APIRouter, HTTPException, Depends, Request, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
import os
import re

from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

from utils.dependencies import verify_token
from .create_master_sku import create_master_sku, MasterSKURequest, _run_dseo_task

# NOTE: This endpoint runs as a synchronous `def` so FastAPI executes it in a
# threadpool. That keeps its blocking work (pymongo + the inline MasterSKU
# creation, which itself does blocking HTTP/DB calls) off the event loop.
# Background SERP enrichment is scheduled inside create_master_sku via
# BackgroundTasks, so this module no longer schedules anything itself.

# ==== HELPERS ====

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def validate_mandatory_fields(data) -> List[str]:
    missing_fields = []
    if not data.ClientKey or not data.ClientKey.strip():
        missing_fields.append("ClientKey")
    if not data.Locale or not data.Locale.strip():
        missing_fields.append("Locale")
    if not data.SKU or not data.SKU.strip():
        missing_fields.append("SKU")
    if not data.Source or not data.Source.strip():
        missing_fields.append("Source")
    if (not data.GTIN or not data.GTIN.strip()) and (not data.Make or not data.Make.strip() or not data.Model or not data.Model.strip()):
        missing_fields.append("GTIN or (Make and Model)")
    return missing_fields

def locale_exists(locale_data_list, locale: str) -> bool:
    return any(entry.get("locale") == locale for entry in locale_data_list)

def find_locale_data(locale_specific_data, locale: str):
    return next((entry for entry in locale_specific_data if entry.get("locale") == locale), {})

def build_identifiers(mastersku, sku: str) -> dict:
    return {
        "GTIN": mastersku.get("GTIN", []),
        "Make": mastersku.get("Make", ""),
        "Model": mastersku.get("Model", ""),
        "SKU": sku
    }

def master_query_for(data) -> Optional[dict]:
    """Build the MasterSKU lookup query from GTIN (preferred) or Make+Model.

    Make/Model are escaped so product codes containing regex metacharacters can't
    break the query or match unintended documents.
    """
    if data.GTIN and data.GTIN.strip():
        return {"GTIN": {"$in": [data.GTIN]}}
    if data.Make and data.Model:
        return {
            "Make": {"$regex": f"^{re.escape(data.Make)}$", "$options": "i"},
            "Model": {"$regex": re.escape(data.Model), "$options": "i"},
        }
    return None

def build_locale_data(
    data, locale_details, locale_info, client_info, mastersku_locale=None
) -> dict:
    def fallback(val, fallback_val):
        if val is not None and val != "" and (not isinstance(val, (int, float)) or val != 0):
            return val
        return fallback_val

    d = {
        "locale": data.Locale,
        "Title": fallback(
            locale_details.Title, mastersku_locale.get("Input_Title") if mastersku_locale else ""
        ),
        "Generate_Offers": "Y",
        "MSRP": fallback(
            locale_details.Price, mastersku_locale.get("Price") if mastersku_locale else 0
        ),
        "Currency": fallback(
            locale_info.get("currency", ""),
            mastersku_locale.get("Currency") if mastersku_locale else "",
        ),
        "created_at": utc_now_iso(),
        "Guarantees": {
            "Labour": (
                locale_details.GTL if locale_details.GTL not in (None, "", 0)
                else locale_info.get("gtee_labour", 0)
            ),
            "Parts": (
                locale_details.GTP if locale_details.GTP not in (None, "", 0)
                else locale_info.get("gtee_parts", 0)
            ),
            "Promotion": locale_details.Promo_Code or "",
        },
        "Custom_Links": (
            [cl.dict() for cl in (locale_details.Custom_Links or [])]
            if getattr(locale_details, "Custom_Links", None)
            else [
                {"Type": "QR", "URL": ""},
                {"Type": "Service", "URL": ""},
                {"Type": "Recycle", "URL": ""},
            ]
        ),
    }
    # Include the localized matched category string from the MasterSKU locale block, if available
    try:
        if mastersku_locale and isinstance(mastersku_locale, dict):
            lm = mastersku_locale.get("Locale_Matched_Category")
            d["Locale_Matched_Category"] = lm if lm not in (None, "") else None
        else:
            d["Locale_Matched_Category"] = None
    except Exception:
        # Don't let localization lookup break creation
        d["Locale_Matched_Category"] = None
    return d

def build_existing_query(client_name, data):
    sku_cond = {
        "Client": client_name,
        "Identifiers.SKU": data.SKU,
        "Sources": {"$in": [data.Source]}
    }
    gtin_cond = (
        {
            "Client": client_name,
            "Identifiers.GTIN": {"$in": [data.GTIN]},
            "Sources": {"$in": [data.Source]}
        }
        if data.GTIN and data.GTIN.strip() else None
    )
    make_model_cond = (
        {
            "Client": client_name,
            "Identifiers.Make": {"$regex": f"^{re.escape(data.Make)}$", "$options": "i"},
            "Identifiers.Model": {"$regex": f"^{re.escape(data.Model)}$", "$options": "i"},
            "Sources": {"$in": [data.Source]}
        }
        if data.Make and data.Model else None
    )
    or_conditions = [sku_cond]
    if gtin_cond: or_conditions.append(gtin_cond)
    if make_model_cond: or_conditions.append(make_model_cond)
    return {"$or": or_conditions}

# ==== END HELPERS ====

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Catalog"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]

locale_collection = db["Locale_Params"]
client_collection = db["ClientKey"]
customsku_collection = db["CustomSKU"]
mastersku_collection = db["MasterSKU"]

class CustomLink(BaseModel):
    Type: str
    URL: str

class LocaleDetails(BaseModel):
    Title: Optional[str] = ""
    Price: Optional[float] = 0
    GTL: Optional[int] = 0
    GTP: Optional[int] = 0
    Promo_Code: Optional[str] = ""
    Custom_Links: Optional[List[CustomLink]] = None

class CustomSKURequest(BaseModel):
    ClientKey: str
    Locale: str
    SKU: str
    Source: str
    GTIN: Optional[str] = ""
    Make: Optional[str] = ""
    Model: Optional[str] = ""
    Category: Optional[str] = ""
    Locale_Details: Optional[LocaleDetails] = None
    Global_Promotion: Optional[str] = None
    add_pricing: Optional[bool] = True


def ensure_master_with_locale(data, request, background_tasks):
    """Return the MasterSKU document that contains data.Locale.

    If a matching MasterSKU already has the locale, it's returned as-is.
    Otherwise create_master_sku is invoked synchronously to create the
    MasterSKU (or add the locale). Because that call commits its write before
    returning, a single re-read is enough — no polling/sleep loop required.

    Returns None only when there are no identifiers (GTIN or Make+Model) to
    match on.
    """
    query = master_query_for(data)
    if not query:
        return None

    master = mastersku_collection.find_one(query)
    if master and locale_exists(master.get("Locale_Specific_Data", []), data.Locale):
        # MasterSKU already has this locale — still fire DSEO pricing if requested
        if data.add_pricing:
            background_tasks.add_task(_run_dseo_task, data.Locale, str(master["_id"]))
        return master

    master_data = MasterSKURequest(
        Make=data.Make or "",
        Model=data.Model or "",
        GTIN=data.GTIN or "",
        locale=data.Locale,
        Category=data.Category,
    )
    # Synchronous call — create_master_sku persists before returning and
    # schedules its own background DataforSEO enrichment via BackgroundTasks.
    create_master_sku(
        master_data,
        request=request,
        background_tasks=background_tasks,
        add_pricing=data.add_pricing,
    )
    return mastersku_collection.find_one(query)


def _serialize(doc):
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


@router.post("/create_custom_sku")
def create_custom_sku(
    data: CustomSKURequest,
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_token),
):
    # 0. Validate inputs
    missing_fields = validate_mandatory_fields(data)
    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing mandatory input(s): {', '.join(missing_fields)}"
        )

    # 1. Lookup locale
    locale_info = locale_collection.find_one({"locale": data.Locale})
    if not locale_info:
        raise HTTPException(status_code=404, detail=f"Locale {data.Locale} not found.")

    # 2. Lookup client
    client_info = client_collection.find_one({"ClientKey": data.ClientKey})
    if not client_info:
        raise HTTPException(status_code=404, detail=f"ClientKey {data.ClientKey} not found.")
    client_name = client_info.get("Client_ID", "")

    # 3. Check for an existing CustomSKU (by SKU, GTIN, or Make+Model)
    existing = customsku_collection.find_one(build_existing_query(client_name, data))

    if existing:
        # 3a. Already has this locale — nothing to do.
        if locale_exists(existing.get("Locale_Specific_Data", []), data.Locale):
            return {
                "message": "SKU exists already for client and locale",
                "existing": _serialize(existing),
            }

        # 3b. Ensure the MasterSKU carries this locale, then append it to the CustomSKU.
        master = ensure_master_with_locale(data, request, background_tasks)
        master_locale = find_locale_data(master.get("Locale_Specific_Data", []), data.Locale) if master else {}
        if not master_locale:
            return {"message": "Master SKU creation is taking longer than expected. Please try again in a few seconds."}

        locale_details = data.Locale_Details or LocaleDetails()
        locale_data = build_locale_data(data, locale_details, locale_info, client_info, mastersku_locale=master_locale)
        customsku_collection.update_one(
            {"_id": existing["_id"]},
            {"$push": {"Locale_Specific_Data": locale_data}},
        )
        # Warm the embeddable-widget quote cache for the newly added locale.
        from routers.widget_quote import warm_widget_cache
        background_tasks.add_task(warm_widget_cache, data.ClientKey, str(existing["_id"]), data.Locale)
        persisted = customsku_collection.find_one({"_id": existing["_id"]})
        return {"message": "Locale added to existing CustomSKU", "customsku": _serialize(persisted)}

    # 4. No existing CustomSKU — ensure the MasterSKU exists, then create the CustomSKU.
    master = ensure_master_with_locale(data, request, background_tasks)
    if master is None:
        return {"message": "No GTIN or Make/Model supplied for MasterSKU matching, unable to proceed."}

    master_locale = find_locale_data(master.get("Locale_Specific_Data", []), data.Locale)
    if not master_locale:
        return {"message": "Master SKU creation is taking longer than expected. Please try again in a few seconds."}

    locale_details = data.Locale_Details or LocaleDetails()
    locale_data = build_locale_data(data, locale_details, locale_info, client_info, mastersku_locale=master_locale)
    category_root = data.Category if data.Category not in (None, "") else master.get("Category", "")

    doc = {
        "Client": client_name,
        "Client_Key": data.ClientKey,
        "Sources": [data.Source],
        "Identifiers": build_identifiers(master, data.SKU),
        "MasterSKU": str(master["_id"]),
        "Category": category_root,
        "Global_Promotion": data.Global_Promotion if data.Global_Promotion is not None else None,
        "Locale_Specific_Data": [locale_data],
    }
    result = customsku_collection.insert_one(doc)
    # Warm the embeddable-widget quote cache so the first shopper is fast too.
    from routers.widget_quote import warm_widget_cache
    background_tasks.add_task(warm_widget_cache, data.ClientKey, str(result.inserted_id), data.Locale)
    persisted = customsku_collection.find_one({"_id": result.inserted_id})
    return _serialize(persisted)
