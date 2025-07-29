from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List, Any
from utils.dependencies import verify_token
from pymongo import MongoClient
from bson import ObjectId
import os
from datetime import datetime

router = APIRouter(tags=["Device Registration"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
clients_collection = db["ClientKey"]
locale_params_collection = db["Locale_Params"]
customsku_collection = db["CustomSKU"]
mastersku_collection = db["MasterSKU"]
devices_collection = db["Devices"]

class IdentifiersModel(BaseModel):
    GTIN: Optional[str] = ""
    make: Optional[str] = ""
    model: Optional[str] = ""
    SKU: Optional[str] = ""
    title: Optional[str] = ""
    category: Optional[str] = ""
    gtee_parts: Optional[str] = ""
    gtee_labour: Optional[str] = ""
    promo: Optional[str] = ""

class UniqueParametersModel(BaseModel):
    MAC: Optional[str] = ""
    serial: Optional[str] = ""
    imei: Optional[Any] = ""
    purchase_date: Optional[str] = ""
    price: Optional[float] = 0
    client_ref: Optional[str] = ""

class DeviceModel(BaseModel):
    Identifiers: IdentifiersModel
    Unique_Parameters: UniqueParametersModel

class SimpleRegisterRequest(BaseModel):
    clientkey: str
    locale: str
    source: str
    Devices: List[DeviceModel]

def valid_value(val):
    return val is not None and str(val).strip() != "" and str(val).strip().lower() != "string"

def validate_purchase_date(date_str):
    if not date_str:
        return True
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False

def validate_mandatory_fields(payload):
    missing_fields = []
    for field in ["clientkey", "locale", "source"]:
        value = getattr(payload, field, None)
        if value is None or str(value).strip() == "" or str(value).strip().lower() == "string":
            missing_fields.append(field)
    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing or invalid required field(s): {', '.join(missing_fields)}"
        )

def lookup_customsku(ids, client_id, locale):
    customsku_doc = None
    if valid_value(ids.SKU):
        customsku_doc = customsku_collection.find_one({
            "Identifiers.SKU": ids.SKU,
            "Client": client_id,
            "Locale_Specific_Data.locale": locale
        })
    if not customsku_doc and valid_value(ids.GTIN):
        customsku_doc = customsku_collection.find_one({
            "Identifiers.GTIN": ids.GTIN,
            "Client": client_id,
            "Locale_Specific_Data.locale": locale
        })
    return customsku_doc

def lookup_mastersku(customsku_doc, locale):
    if not customsku_doc or "MasterSKU" not in customsku_doc:
        return None
    try:
        master_id = customsku_doc["MasterSKU"]
        if isinstance(master_id, str):
            master_id = ObjectId(master_id)
        mastersku_doc = mastersku_collection.find_one({
            "_id": master_id,
            "Locale_Specific_Data.locale": locale
        })
        if not mastersku_doc:
            mastersku_doc = mastersku_collection.find_one({
                "_id": master_id
            })
        return mastersku_doc
    except Exception:
        return None

def extract_locale_specific_data(doc, locale):
    if not doc or "Locale_Specific_Data" not in doc:
        return None
    lsd = doc["Locale_Specific_Data"]
    entry = next((item for item in lsd if item.get("locale") == locale), None)
    return entry

def get_first_non_blank(*args):
    for val in args:
        if val is None:
            continue
        if isinstance(val, list):
            for v in val:
                if v and str(v).strip() and str(v).strip().lower() != "string":
                    return v
        elif str(val).strip() and str(val).strip().lower() != "string":
            return val
    return ""

def price_is_missing(val):
    try:
        if val is None or str(val).strip() == "" or str(val).strip().lower() == "string":
            return True
        return float(val) == 0
    except Exception:
        return True

@router.post("/device-register")
def device_register(payload: SimpleRegisterRequest, _: None = Depends(verify_token)):
    validate_mandatory_fields(payload)

    client_doc = clients_collection.find_one({"ClientKey": payload.clientkey})
    if not client_doc:
        raise HTTPException(status_code=400, detail="Invalid clientkey.")
    locale_doc = locale_params_collection.find_one({"locale": payload.locale})
    if not locale_doc:
        raise HTTPException(status_code=400, detail="Locale is not supported in system.")

    client_id = client_doc.get("Client_ID")
    if not client_id:
        raise HTTPException(status_code=400, detail="Client_ID not found in client document.")

    inserted = []

    for device in payload.Devices:
        ids = device.Identifiers
        unique = device.Unique_Parameters

        duplicate_query = []
        if valid_value(unique.imei):
            duplicate_query.append({"uniqueParameters.imei": unique.imei})
        if valid_value(unique.MAC):
            duplicate_query.append({"uniqueParameters.MAC": unique.MAC})
        if valid_value(ids.make) and valid_value(ids.model) and valid_value(unique.serial):
            duplicate_query.append({
                "identifiers.make": ids.make,
                "identifiers.model": ids.model,
                "uniqueParameters.serial": unique.serial
            })
        if valid_value(ids.GTIN) and valid_value(unique.serial):
            duplicate_query.append({
                "identifiers.GTIN": ids.GTIN,
                "uniqueParameters.serial": unique.serial
            })

        duplicate_device = None
        matched_field = None
        matched_value = None

        for query in duplicate_query:
            found = devices_collection.find_one(query)
            if found:
                duplicate_device = found
                if "uniqueParameters.imei" in query:
                    matched_field = "imei"
                    matched_value = unique.imei
                elif "uniqueParameters.MAC" in query:
                    matched_field = "MAC"
                    matched_value = unique.MAC
                elif "identifiers.GTIN" in query:
                    matched_field = "GTIN/serial"
                    matched_value = f"{ids.GTIN} / {unique.serial}"
                else:
                    matched_field = "make/model/serial"
                    matched_value = f"{ids.make} / {ids.model} / {unique.serial}"
                break

        if duplicate_device:
            inserted.append({
                "deviceId": str(duplicate_device["_id"]),
                "skuStatus": "duplicate record found",
                "matched_field": matched_field,
                "matched_value": matched_value
            })
            continue

        if unique.purchase_date and not validate_purchase_date(unique.purchase_date):
            inserted.append({
                "skuStatus": "error",
                "detail": "Invalid purchase date format. Should be YYYY-MM-DD (e.g. 2025-05-01).",
                "Identifiers": ids.dict(),
                "Unique_Parameters": unique.dict(),
                "registeredAt": datetime.utcnow().isoformat() + "Z"
            })
            continue

        identification_ok = False
        if valid_value(ids.GTIN) and str(ids.GTIN).strip() != "0":
            identification_ok = True
        elif valid_value(ids.make) and valid_value(ids.model):
            identification_ok = True
        elif valid_value(ids.SKU):
            identification_ok = True

        if not identification_ok:
            inserted.append({
                "skuStatus": "error",
                "detail": "You must provide a valid GTIN (not '', null, or '0'), or valid Make AND Model (not '', 'string', or null), or valid SKU (not '', 'string', or null).",
                "Identifiers": ids.dict(),
                "Unique_Parameters": unique.dict(),
                "registeredAt": datetime.utcnow().isoformat() + "Z"
            })
            continue

        customsku_doc = lookup_customsku(ids, client_id, payload.locale)
        customsku_id = str(customsku_doc["_id"]) if customsku_doc else None
        lsd_custom = extract_locale_specific_data(customsku_doc, payload.locale) if customsku_doc else None

        mastersku_doc = lookup_mastersku(customsku_doc, payload.locale)
        mastersku_id = str(mastersku_doc["_id"]) if mastersku_doc and "_id" in mastersku_doc else None
        lsd_master = extract_locale_specific_data(mastersku_doc, payload.locale) if mastersku_doc else None

        identifiers = {
            "GTIN": get_first_non_blank(
                ids.GTIN,
                customsku_doc.get("Identifiers", {}).get("GTIN") if customsku_doc else None,
                mastersku_doc.get("GTIN") if mastersku_doc else None
            ),
            "make": get_first_non_blank(
                ids.make,
                customsku_doc.get("Identifiers", {}).get("Make") if customsku_doc else None,
                lsd_custom.get("Make") if lsd_custom else None,
                mastersku_doc.get("Make") if mastersku_doc else None,
                lsd_master.get("Make") if lsd_master else None
            ),
            "model": get_first_non_blank(
                ids.model,
                customsku_doc.get("Identifiers", {}).get("Model") if customsku_doc else None,
                lsd_custom.get("Model") if lsd_custom else None,
                mastersku_doc.get("Model") if mastersku_doc else None,
                lsd_master.get("Model") if lsd_master else None
            ),
            "SKU": get_first_non_blank(
                ids.SKU,
                customsku_doc.get("Identifiers", {}).get("SKU") if customsku_doc else None,
                lsd_custom.get("SKU") if lsd_custom else None,
                mastersku_doc.get("Productname") if mastersku_doc else None,
                lsd_master.get("SKU") if lsd_master else None
            ),
            "title": get_first_non_blank(
                ids.title,
                lsd_custom.get("Title") if lsd_custom else None,
                customsku_doc.get("Title") if customsku_doc else None,
                mastersku_doc.get("Title") if mastersku_doc else None,
                lsd_master.get("Title") if lsd_master else None
            ),
            "category": get_first_non_blank(
                ids.category,
                lsd_custom.get("Category") if lsd_custom else None,
                customsku_doc.get("Category") if customsku_doc else None,
                mastersku_doc.get("Category") if mastersku_doc else None,
                lsd_master.get("Category") if lsd_master else None,
                mastersku_doc.get("Matched_Category") if mastersku_doc else None
            ),
            "gteeParts": get_first_non_blank(
                ids.gtee_parts,
                lsd_custom.get("Guarantees", {}).get("Parts") if lsd_custom and lsd_custom.get("Guarantees") else None
            ),
            "gteeLabour": get_first_non_blank(
                ids.gtee_labour,
                lsd_custom.get("Guarantees", {}).get("Labour") if lsd_custom and lsd_custom.get("Guarantees") else None
            ),
            "promo": get_first_non_blank(
                ids.promo,
                lsd_custom.get("Guarantees", {}).get("Promotion") if lsd_custom and lsd_custom.get("Guarantees") else None
            )
        }

        unique_parameters = {
            "MAC": unique.MAC or "",
            "serial": unique.serial or "",
            "imei": unique.imei if unique.imei and unique.imei != "string" else ""
        }

        if price_is_missing(unique.price):
            price = lsd_custom.get("MSRP") if lsd_custom and lsd_custom.get("MSRP") is not None else None
            if price_is_missing(price):
                price = lsd_master.get("Price") if lsd_master and lsd_master.get("Price") is not None else None
            price = float(price) if not price_is_missing(price) else 0
        else:
            price = float(unique.price)

        registration_parameters = {
            "purchaseDate": unique.purchase_date or "",
            "price": price,
            "clientRef": unique.client_ref or "",
            "registrationStatus": "unassigned"
        }

        matched_status = "matched" if (customsku_doc or mastersku_doc) else "no match"

        if matched_status != "matched":
            inserted.append({
                "skuStatus": "error",
                "detail": "Device enrichment did not find a matching CustomSKU or MasterSKU. No document created.",
                "Identifiers": ids.dict(),
                "Unique_Parameters": unique.dict(),
                "registeredAt": datetime.utcnow().isoformat() + "Z"
            })
            continue

        device_doc = {
            "client": client_id,
            "locale": payload.locale,
            "source": payload.source,
            "identifiers": identifiers,
            "uniqueParameters": unique_parameters,
            "registrationParameters": registration_parameters,
            "customSkuId": customsku_id,
            "masterSkuId": mastersku_id,
            "skuStatus": matched_status,
            "registeredAt": datetime.utcnow().isoformat() + "Z"
        }

        result = devices_collection.insert_one(device_doc)
        device_doc["_id"] = str(result.inserted_id)

        inserted.append({
            "deviceId": device_doc["_id"],
            "skuStatus": matched_status
        })

    return {
        "inserted": inserted,
        "count": len(inserted)
    }
