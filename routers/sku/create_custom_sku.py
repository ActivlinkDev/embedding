# create_custom_sku.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
import os
import time

from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

from utils.dependencies import verify_token
from .create_master_sku import create_master_sku, MasterSKURequest

# === QR CODE SUPPORT ===
import qrcode
import io
import base64

def generate_qr_code_base64(url: str) -> str:
    qr = qrcode.make(url)
    buffered = io.BytesIO()
    qr.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    return img_str
# === END QR CODE SUPPORT ===

# ==== HELPERS ====

def utc_now_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

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
            "Identifiers.Make": {"$regex": f"^{data.Make}$", "$options": "i"},
            "Identifiers.Model": {"$regex": f"^{data.Model}$", "$options": "i"},
            "Sources": {"$in": [data.Source]}
        }
        if data.Make and data.Model else None
    )
    or_conditions = [sku_cond]
    if gtin_cond: or_conditions.append(gtin_cond)
    if make_model_cond: or_conditions.append(make_model_cond)
    return {"$or": or_conditions}

def wait_for_mastersku(mastersku_collection, query, locale, timeout=7, poll_interval=0.7):
    start_time = time.time()
    while (time.time() - start_time) < timeout:
        mastersku = mastersku_collection.find_one(query)
        if mastersku and locale_exists(mastersku.get("Locale_Specific_Data", []), locale):
            return mastersku
        time.sleep(poll_interval)
    return None

# ==== END HELPERS ====

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["SKU"]
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

@router.post("/create_custom_sku")
def create_custom_sku(data: CustomSKURequest, _: None = Depends(verify_token)):
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

    # 3. Check for existing CustomSKU (by any of SKU, GTIN, or Make+Model)
    existing_query = build_existing_query(client_name, data)
    existing = customsku_collection.find_one(existing_query)
    if existing:
        str_existing_id = str(existing["_id"])
        locale_specific_data = existing.get("Locale_Specific_Data", [])
        locale_match = locale_exists(locale_specific_data, data.Locale) if locale_specific_data else False

        if locale_match:
            existing["_id"] = str(existing["_id"])
            # === Add QR Code URL for existing ===
            qr_url = f"http://www.activlink.io/qr?{existing['_id']}"
            qr_code_base64 = generate_qr_code_base64(qr_url)
            # Persist qr_code_image into the existing document in MongoDB
            customsku_collection.update_one(
                {"_id": ObjectId(existing["_id"])},
                {"$set": {"qr_code_image": f"data:image/png;base64,{qr_code_base64}"}}
            )
            # Refresh the document to return the stored value
            persisted = customsku_collection.find_one({"_id": ObjectId(existing["_id"])})
            if persisted:
                persisted["_id"] = str(persisted["_id"])
                return {
                    "message": "SKU exists already for client and locale",
                    "existing": persisted
                }
            else:
                # fallback to returning the in-memory object with qr_code_image attached
                existing["qr_code_image"] = f"data:image/png;base64,{qr_code_base64}"
                return {
                    "message": "SKU exists already for client and locale",
                    "existing": existing
                }
        else:
            # Only add the locale to CustomSKU if MasterSKU has it, else trigger creation and poll
            mastersku_query = None
            if data.GTIN and data.GTIN.strip():
                mastersku_query = {"GTIN": {"$in": [data.GTIN]}}
            elif data.Make and data.Model:
                mastersku_query = {
                    "Make": {"$regex": f"^{data.Make}$", "$options": "i"},
                    "Model": {"$regex": data.Model, "$options": "i"}
                }
            mastersku = mastersku_collection.find_one(mastersku_query) if mastersku_query else None
            mastersku_locale_data = find_locale_data(
                mastersku.get("Locale_Specific_Data", []), data.Locale
            ) if mastersku else {}

            if not mastersku_locale_data:
                # Locale does not exist in MasterSKU, create and poll
                master_data = MasterSKURequest(
                    Make=data.Make,
                    Model=data.Model,
                    GTIN=data.GTIN,
                    locale=data.Locale,
                    Category=data.Category
                )
                create_master_sku(master_data)
                mastersku = wait_for_mastersku(mastersku_collection, mastersku_query, data.Locale)
                mastersku_locale_data = find_locale_data(
                    mastersku.get("Locale_Specific_Data", []), data.Locale
                ) if mastersku else {}
                if not mastersku_locale_data:
                    return {
                        "message": "Master SKU creation is taking longer than expected. Please try again in a few seconds."
                    }
            # Locale exists in MasterSKU - proceed to add to CustomSKU
            locale_details = data.Locale_Details or LocaleDetails()
            locale_data = build_locale_data(
                data, locale_details, locale_info, client_info, mastersku_locale=mastersku_locale_data
            )
            customsku_collection.update_one(
                {"_id": existing["_id"]},
                {"$push": {"Locale_Specific_Data": locale_data}}
            )
            updated_doc = customsku_collection.find_one({"_id": ObjectId(str_existing_id)})
            if updated_doc:
                updated_doc["_id"] = str(updated_doc["_id"])
                # === Generate and persist QR Code for updated document ===
                qr_url = f"http://www.activlink.io/qr?{updated_doc['_id']}"
                qr_code_base64 = generate_qr_code_base64(qr_url)
                customsku_collection.update_one(
                    {"_id": ObjectId(str_existing_id)},
                    {"$set": {"qr_code_image": f"data:image/png;base64,{qr_code_base64}"}}
                )
                # Refresh
                persisted = customsku_collection.find_one({"_id": ObjectId(str_existing_id)})
                if persisted:
                    persisted["_id"] = str(persisted["_id"])
                    return {
                        "message": "Locale added to existing CustomSKU",
                        "customsku": persisted
                    }
                # fallback
                updated_doc["qr_code_image"] = f"data:image/png;base64,{qr_code_base64}"
                return {
                    "message": "Locale added to existing CustomSKU",
                    "customsku": updated_doc
                }
            else:
                raise HTTPException(status_code=500, detail="Failed to retrieve updated CustomSKU document.")

    # 4. If no CustomSKU exists, search MasterSKU by GTIN or Make/Model
    mastersku_query = None
    if data.GTIN and data.GTIN.strip():
        mastersku_query = {"GTIN": {"$in": [data.GTIN]}}
    elif data.Make and data.Model:
        mastersku_query = {
            "Make": {"$regex": f"^{data.Make}$", "$options": "i"},
            "Model": {"$regex": data.Model, "$options": "i"}
        }

    if mastersku_query:
        mastersku = mastersku_collection.find_one(mastersku_query)
        if mastersku:
            mastersku_id = str(mastersku["_id"])
            mastersku["_id"] = mastersku_id
            locale_found = locale_exists(mastersku.get("Locale_Specific_Data", []), data.Locale)
            if locale_found:
                identifiers = build_identifiers(mastersku, data.SKU)
                locale_details = data.Locale_Details or LocaleDetails()
                mastersku_locale_data = find_locale_data(
                    mastersku.get("Locale_Specific_Data", []), data.Locale
                )
                locale_data = build_locale_data(
                    data, locale_details, locale_info, client_info, mastersku_locale=mastersku_locale_data
                )
                # Set root Category: prefer input, else MasterSKU root, else ""
                category_root = (
                    data.Category if data.Category not in (None, "")
                    else mastersku.get("Category", "")
                )
                doc = {
                    "Client": client_name,
                    "Client_Key": data.ClientKey,
                    "Sources": [data.Source],
                    "Identifiers": identifiers,
                    "MasterSKU": mastersku_id,
                    "Category": category_root,
                    "Global_Promotion": data.Global_Promotion if getattr(data, 'Global_Promotion', None) is not None else None,
                    "Locale_Specific_Data": [locale_data],
                }
                result = customsku_collection.insert_one(doc)
                doc["_id"] = str(result.inserted_id)
                # === Generate and persist QR Code for new doc ===
                qr_url = f"http://www.activlink.io/qr?{doc['_id']}"
                qr_code_base64 = generate_qr_code_base64(qr_url)
                customsku_collection.update_one(
                    {"_id": ObjectId(doc["_id"])},
                    {"$set": {"qr_code_image": f"data:image/png;base64,{qr_code_base64}"}}
                )
                # Refresh
                persisted = customsku_collection.find_one({"_id": ObjectId(doc["_id"])})
                if persisted:
                    persisted["_id"] = str(persisted["_id"])
                    return persisted
                # fallback
                doc["qr_code_image"] = f"data:image/png;base64,{qr_code_base64}"
                return doc
            else:
                # Call master sku creation to add this locale, and poll
                master_data = MasterSKURequest(
                    Make=data.Make,
                    Model=data.Model,
                    GTIN=data.GTIN,
                    locale=data.Locale,
                    Category=data.Category
                )
                create_master_sku(master_data)
                mastersku = wait_for_mastersku(mastersku_collection, mastersku_query, data.Locale)
                if not mastersku:
                    return {
                        "message": "Master SKU creation is taking longer than expected. Please try again in a few seconds."
                    }
                # After polling, proceed to CustomSKU creation
                mastersku_id = str(mastersku["_id"])
                identifiers = build_identifiers(mastersku, data.SKU)
                locale_details = data.Locale_Details or LocaleDetails()
                mastersku_locale_data = find_locale_data(
                    mastersku.get("Locale_Specific_Data", []), data.Locale
                )
                locale_data = build_locale_data(
                    data, locale_details, locale_info, client_info, mastersku_locale=mastersku_locale_data
                )
                category_root = (
                    data.Category if data.Category not in (None, "")
                    else mastersku.get("Category", "")
                )
                doc = {
                    "Client": client_name,
                    "Client_Key": data.ClientKey,
                    "Sources": [data.Source],
                    "Identifiers": identifiers,
                    "MasterSKU": mastersku_id,
                    "Category": category_root,
                    "Global_Promotion": data.Global_Promotion if getattr(data, 'Global_Promotion', None) is not None else None,
                    "Locale_Specific_Data": [locale_data],
                }
                result = customsku_collection.insert_one(doc)
                doc["_id"] = str(result.inserted_id)
                qr_url = f"http://www.activlink.io/qr?{doc['_id']}"
                qr_code_base64 = generate_qr_code_base64(qr_url)
                customsku_collection.update_one(
                    {"_id": ObjectId(doc["_id"])},
                    {"$set": {"qr_code_image": f"data:image/png;base64,{qr_code_base64}"}}
                )
                persisted = customsku_collection.find_one({"_id": ObjectId(doc["_id"])})
                if persisted:
                    persisted["_id"] = str(persisted["_id"])
                    return persisted
                doc["qr_code_image"] = f"data:image/png;base64,{qr_code_base64}"
                return doc
        else:
            # No master SKU found, create master SKU and poll
            master_data = MasterSKURequest(
                Make=data.Make,
                Model=data.Model,
                GTIN=data.GTIN,
                locale=data.Locale,
                Category=data.Category
            )
            create_master_sku(master_data)
            mastersku = wait_for_mastersku(mastersku_collection, mastersku_query, data.Locale)
            if not mastersku:
                return {
                    "message": "Master SKU creation is taking longer than expected. Please try again in a few seconds."
                }
            # After polling, proceed to CustomSKU creation
            mastersku_id = str(mastersku["_id"])
            identifiers = build_identifiers(mastersku, data.SKU)
            locale_details = data.Locale_Details or LocaleDetails()
            mastersku_locale_data = find_locale_data(
                mastersku.get("Locale_Specific_Data", []), data.Locale
            )
            locale_data = build_locale_data(
                data, locale_details, locale_info, client_info, mastersku_locale=mastersku_locale_data
            )
            category_root = (
                data.Category if data.Category not in (None, "")
                else mastersku.get("Category", "")
            )
            doc = {
                "Client": client_name,
                "Client_Key": data.ClientKey,
                "Sources": [data.Source],
                "Identifiers": identifiers,
                "MasterSKU": mastersku_id,
                "Category": category_root,
                "Global_Promotion": data.Global_Promotion if getattr(data, 'Global_Promotion', None) is not None else None,
                "Locale_Specific_Data": [locale_data],
            }
            result = customsku_collection.insert_one(doc)
            doc["_id"] = str(result.inserted_id)
            qr_url = f"http://www.activlink.io/qr?{doc['_id']}"
            qr_code_base64 = generate_qr_code_base64(qr_url)
            customsku_collection.update_one(
                {"_id": ObjectId(doc["_id"])},
                {"$set": {"qr_code_image": f"data:image/png;base64,{qr_code_base64}"}}
            )
            persisted = customsku_collection.find_one({"_id": ObjectId(doc["_id"] )})
            if persisted:
                persisted["_id"] = str(persisted["_id"])
                return persisted
            doc["qr_code_image"] = f"data:image/png;base64,{qr_code_base64}"
            return doc

    return {
        "message": "No GTIN or Make/Model supplied for MasterSKU matching, unable to proceed."
    }
