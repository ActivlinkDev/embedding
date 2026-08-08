from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional, List, Any
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from pymongo import MongoClient
from bson import ObjectId
import os
from datetime import datetime

router = APIRouter(tags=["Devices"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
clients_collection = db["ClientKey"]
locale_params_collection = db["Locale_Params"]
customsku_collection = db["CustomSKU"]
mastersku_collection = db["MasterSKU"]
devices_collection = db["Devices"]

class IdentifiersModel(BaseModel):
    """What kind of product this is — used to match the device to a CustomSKU/MasterSKU.

    Every field is optional individually, but **at least one identification route must be
    complete**: a `GTIN` (not `"0"`), or both `make` and `model`, or a `SKU`. A device that
    satisfies none of them is returned with `skuStatus: "error"` and is not stored.

    Values left blank are back-filled from the matched catalogue record where one is found, so
    sending only a GTIN is normal — `title`, `category` and the guarantee fields will be
    populated from the catalogue.
    """

    GTIN: Optional[str] = Field(
        "",
        description="Barcode / GTIN-13. The strongest identifier. `\"0\"` counts as absent.",
        examples=["5011773057240"],
    )
    make: Optional[str] = Field(
        "",
        description="Manufacturer name. Matches a SKU only in combination with `model`.",
        examples=["Bosch"],
    )
    model: Optional[str] = Field(
        "",
        description="Manufacturer model designation. Matches a SKU only in combination with `make`.",
        examples=["SMS6ZCI00G"],
    )
    SKU: Optional[str] = Field(
        "",
        description="The client's own SKU code, if it is already known to the catalogue.",
        examples=["BOSCH-DW-4421"],
    )
    title: Optional[str] = Field(
        "",
        description="Display name. Taken from the matched catalogue record when omitted.",
        examples=["Bosch Series 6 Freestanding Dishwasher"],
    )
    category: Optional[str] = Field(
        "",
        description="Product category. Taken from the matched catalogue record when omitted.",
        examples=["Dishwasher"],
    )
    gtee_parts: Optional[str] = Field(
        "",
        description="Manufacturer parts guarantee in months. Falls back to the catalogue value.",
        examples=["24"],
    )
    gtee_labour: Optional[str] = Field(
        "",
        description="Manufacturer labour guarantee in months. Falls back to the catalogue value.",
        examples=["12"],
    )
    promo: Optional[str] = Field(
        "",
        description="Promotional guarantee extension, if the catalogue record carries one.",
        examples=["+12 months registration promotion"],
    )


class UniqueParametersModel(BaseModel):
    """Facts about the individual unit — what distinguishes this device from another of the
    same product. These drive duplicate detection and the price used for rating."""

    MAC: Optional[str] = Field(
        "",
        description="MAC address, where the device has one. Checked for duplicates second.",
        examples=["A4:83:E7:2B:19:0C"],
    )
    serial: Optional[str] = Field(
        "",
        description=(
            "Manufacturer serial number. Only detects duplicates in combination with "
            "`make`+`model` or `GTIN`."
        ),
        examples=["SN-8841203"],
    )
    imei: Optional[Any] = Field(
        "",
        description=(
            "IMEI for cellular devices. Checked for duplicates first. The literal string "
            "`\"string\"` is discarded rather than stored."
        ),
        examples=["356938035643809"],
    )
    purchase_date: Optional[str] = Field(
        "",
        description=(
            "Date of purchase in **`YYYY-MM-DD`** format. Any other format fails that single "
            "device with `skuStatus: \"error\"` — the rest of the batch still registers."
        ),
        examples=["2025-05-01"],
    )
    price: Optional[float] = Field(
        0,
        description=(
            "Purchase price in the locale's currency. When omitted or `0`, the price falls back "
            "to the CustomSKU `MSRP`, then the MasterSKU `Price`, then `0`."
        ),
        examples=[449.99],
    )
    client_ref: Optional[str] = Field(
        "",
        description="Your own reference for this registration (order number, line id, …).",
        examples=["ORD-2026-00918"],
    )


class DeviceModel(BaseModel):
    """One device to register. Both sections are mandatory, though every field inside them
    is individually optional."""

    Identifiers: IdentifiersModel = Field(..., description="**Mandatory.** What the product is.")
    Unique_Parameters: UniqueParametersModel = Field(
        ..., description="**Mandatory.** Which individual unit this is."
    )


class SimpleRegisterRequest(BaseModel):
    """A batch registration for one tenant and locale."""

    clientkey: str = Field(
        ...,
        description=(
            "**Mandatory.** Tenant key; must match a `ClientKey` record. Blank, `null` or the "
            "literal `\"string\"` are rejected with `400`, as is a key that does not exist."
        ),
        examples=["acme_uk_live"],
    )
    locale: str = Field(
        ...,
        description=(
            "**Mandatory.** Locale code such as `en_GB`. Must exist in `Locale_Params` — it "
            "supplies the currency stored against each registration. Unsupported values are "
            "rejected with `400`."
        ),
        examples=["en_GB"],
    )
    source: str = Field(
        ...,
        description=(
            "**Mandatory.** Where the registration came from, recorded on each device, e.g. "
            "`web`, `pos`, `csv_import`."
        ),
        examples=["web"],
    )
    Devices: List[DeviceModel] = Field(
        ...,
        description="**Mandatory.** One or more devices to register in a single call.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "clientkey": "acme_uk_live",
                "locale": "en_GB",
                "source": "web",
                "Devices": [
                    {
                        "Identifiers": {
                            "GTIN": "5011773057240",
                            "make": "Bosch",
                            "model": "SMS6ZCI00G",
                            "SKU": "",
                            "title": "",
                            "category": "",
                            "gtee_parts": "",
                            "gtee_labour": "",
                            "promo": "",
                        },
                        "Unique_Parameters": {
                            "MAC": "",
                            "serial": "SN-8841203",
                            "imei": "",
                            "purchase_date": "2025-05-01",
                            "price": 449.99,
                            "client_ref": "ORD-2026-00918",
                        },
                    }
                ],
            }
        }
    }

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
    # Try by SKU
    if valid_value(ids.SKU):
        customsku_doc = customsku_collection.find_one({
            "Identifiers.SKU": ids.SKU,
            "Client": client_id,
            "Locale_Specific_Data.locale": locale
        })
    # Try by GTIN (GTIN is an array in your data, so use $in)
    if not customsku_doc and valid_value(ids.GTIN):
        customsku_doc = customsku_collection.find_one({
            "Identifiers.GTIN": { "$in": [ids.GTIN] },
            "Client": client_id,
            "Locale_Specific_Data.locale": locale
        })
    # Fallback to make and model
    if not customsku_doc and valid_value(ids.make) and valid_value(ids.model):
        customsku_doc = customsku_collection.find_one({
            "Identifiers.Make": ids.make,
            "Identifiers.Model": ids.model,
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

# --------- Fixed here: Accepts missing or zero as missing ---------
def price_is_missing(val):
    try:
        if val is None or str(val).strip() == "" or str(val).strip().lower() == "string":
            return True
        return float(val) == 0
    except Exception:
        return True
# ------------------------------------------------------------

@router.post(
    "/device-register",
    summary="Register a batch of devices for a client tenant",
    response_description="One outcome entry per device, in the order submitted.",
    responses=secured({
        200: json_response(
            "The batch was processed. Inspect each entry: a device may be newly registered, "
            "recognised as a duplicate, or rejected — all three appear here with HTTP 200.",
            {
                "inserted": [
                    {"deviceId": "6820f1c9a4b21d0f8c9e4471", "skuStatus": "matched"},
                    {
                        "deviceId": "6820f1c9a4b21d0f8c9e4470",
                        "skuStatus": "duplicate record found",
                        "matched_field": "make/model/serial",
                        "matched_value": "Bosch / SMS6ZCI00G / SN-8841203",
                    },
                    {
                        "skuStatus": "error",
                        "detail": "Device enrichment did not find a matching CustomSKU or MasterSKU. No document created.",
                        "Identifiers": {"GTIN": "0000000000000", "make": "", "model": ""},
                        "Unique_Parameters": {"serial": "SN-0000001", "price": 0},
                        "registeredAt": "2026-08-06T10:14:52.113000Z",
                    },
                ],
                "count": 3,
            },
        ),
        400: error(
            "A mandatory top-level field was blank or a placeholder, the `clientkey` is not a "
            "known client, or the `locale` is not supported.",
            "Invalid clientkey.",
        ),
    }),
)
def device_register(payload: SimpleRegisterRequest, _: None = Depends(verify_token)):
    """
    Register one or more devices against a client tenant, enriching each from the SKU catalogue.

    **Per device the endpoint:**

    1. **Checks for duplicates**, in this order — `imei`, then `MAC`, then `make`+`model`+`serial`,
       then `GTIN`+`serial`. A hit returns the existing `deviceId` with
       `skuStatus: "duplicate record found"` plus the `matched_field` that caught it; nothing new
       is stored.
    2. **Validates identification** — a usable `GTIN` (not `"0"`), or `make` **and** `model`, or a
       `SKU`. Failing this returns `skuStatus: "error"` for that device only.
    3. **Enriches from the catalogue** — resolves a CustomSKU for this client and locale, then its
       MasterSKU, and back-fills any blank identifier (title, category, guarantees) from them. A
       device that matches neither is **not stored** and comes back as `skuStatus: "error"`.
    4. **Resolves the price** — the submitted `price`, else the CustomSKU `MSRP`, else the
       MasterSKU `Price`, else `0`. Currency always comes from `Locale_Params`, never the caller.

    **Partial success is normal.** The call returns `200` as long as the tenant and locale are
    valid; individual failures are reported per device inside `inserted`. `count` is the length of
    that array — the number of devices *processed*, not the number stored. Devices are stored with
    `registrationStatus: "unassigned"`; assign cover with
    `GET /assign_product_for_device/{device_id}`.

    A `400` means nothing at all was processed: a blank mandatory field, an unknown `clientkey`,
    or an unsupported `locale`.
    """
    validate_mandatory_fields(payload)

    # 1. Lookup client document by clientkey (MUST match exactly)
    client_doc = clients_collection.find_one({"ClientKey": payload.clientkey})
    if not client_doc:
        raise HTTPException(status_code=400, detail="Invalid clientkey.")

    # 2. Extract Client_ID from the client document
    client_id = client_doc.get("Client_ID")
    if not client_id:
        raise HTTPException(status_code=400, detail="Client_ID not found in client document.")

    # 3. Lookup locale doc for currency etc.
    locale_doc = locale_params_collection.find_one({"locale": payload.locale})
    if not locale_doc:
        raise HTTPException(status_code=400, detail="Locale is not supported in system.")

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

        # --- Use Client_ID from ClientKey for lookups ---
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

        # ---- PRICE FALLBACK LOGIC ----
        if price_is_missing(unique.price):
            price = None
            if lsd_custom and not price_is_missing(lsd_custom.get("MSRP")):
                price = float(lsd_custom.get("MSRP"))
            elif lsd_master and not price_is_missing(lsd_master.get("Price")):
                price = float(lsd_master.get("Price"))
            else:
                price = 0
        else:
            price = float(unique.price)
        # -----------------------------

        registration_parameters = {
            "purchaseDate": unique.purchase_date or "",
            "price": price,
            "currency": locale_doc.get("currency", ""),   # currency comes from Locale_Params
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
