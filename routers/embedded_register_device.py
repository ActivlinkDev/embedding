from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Any
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from pymongo import MongoClient
from bson import ObjectId
import os
from datetime import datetime
import random
import string
import uuid
import qrcode
import io
import base64
import re

router = APIRouter(
    tags=["Devices"]
)

# MongoDB connection setup
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
clients_collection = db["ClientKey"]
locale_params_collection = db["Locale_Params"]
customsku_collection = db["CustomSKU"]
mastersku_collection = db["MasterSKU"]
registrations_collection = db["Registrations"]
registrations_error_log_collection = db["Registrations_Error_Log"]

# ---------- Pydantic Models ----------

EMAIL_REGEX = re.compile(r"(^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$)")

class IdentifiersModel(BaseModel):
    """What kind of product this is. Individually optional, but a device needs **one complete
    identification route** — a `GTIN` (not `"0"`), or `make` **and** `model`, or a `SKU` —
    otherwise it is logged as an error instead of registered."""

    GTIN: Optional[str] = Field("", description="Barcode / GTIN-13. `\"0\"` counts as absent.", examples=["5011773057240"])
    make: Optional[str] = Field("", description="Manufacturer name. Identifies a product only together with `model`.", examples=["Bosch"])
    model: Optional[str] = Field("", description="Model designation. Identifies a product only together with `make`.", examples=["SMS6ZCI00G"])
    SKU: Optional[str] = Field("", description="The client's own SKU code, if already in the catalogue.", examples=["BOSCH-DW-4421"])
    code: Optional[str] = Field("", description="Optional free-form code carried through to the stored registration.", examples=["PROMO-Q3"])
    title: Optional[str] = Field("", description="Display name. Back-filled from the catalogue when blank.", examples=["Bosch Series 6 Freestanding Dishwasher"])
    category: Optional[str] = Field("", description="Product category. Back-filled from the catalogue when blank.", examples=["Dishwasher"])
    gtee_parts: Optional[str] = Field("", description="Manufacturer parts guarantee in months.", examples=["24"])
    id: Optional[str] = Field("", description="Optional caller-side identifier echoed back unchanged.", examples=["line-1"])
    gtee_labour: Optional[str] = Field("", description="Manufacturer labour guarantee in months.", examples=["12"])
    promo: Optional[str] = Field("", description="Promotional guarantee extension from the catalogue record.", examples=["+12 months registration promotion"])


class UniqueParametersModel(BaseModel):
    """Facts about the individual unit being registered."""

    MAC: Optional[str] = Field("", description="MAC address, where the device has one.", examples=["A4:83:E7:2B:19:0C"])
    serial: Optional[str] = Field("", description="Manufacturer serial number.", examples=["SN-8841203"])
    imei: Optional[Any] = Field(None, description="IMEI for cellular devices.", examples=["356938035643809"])
    purchase_date: Optional[str] = Field(
        "",
        description="Date of purchase, **`YYYY-MM-DD`**. Any other format fails that device only.",
        examples=["2025-05-01"],
    )
    price: Optional[float] = Field(
        0,
        description=(
            "Purchase price in the locale's currency. When omitted or `0`, falls back to the "
            "CustomSKU `MSRP`, then the MasterSKU `Price`."
        ),
        examples=[449.99],
    )
    client_ref: Optional[str] = Field("", description="Your own reference for this registration.", examples=["ORD-2026-00918"])


class Customer(BaseModel):
    """Optional end-customer details captured alongside the registration. Every field is
    optional, but `email` — when supplied and non-blank — must be a valid address or the whole
    request is rejected with `422`."""

    Opt_SMS: Optional[bool] = Field(None, description="Customer consented to SMS contact.", examples=[True])
    Opt_email: Optional[bool] = Field(None, description="Customer consented to email contact.", examples=[True])
    name: Optional[str] = Field("", description="Customer's full name.", examples=["Jane Okafor"])
    email: Optional[str] = Field(
        "",
        description="Blank, or a valid email address. Anything else fails validation with `422`.",
        examples=["jane.okafor@example.com"],
    )
    phone: Optional[str] = Field("", description="Contact phone number, ideally in E.164 form.", examples=["+447700900123"])

    @field_validator("email")
    def validate_email_or_blank(cls, v):
        if v in (None, ""):
            return v
        if not EMAIL_REGEX.match(v):
            raise ValueError("Invalid email address format.")
        return v

class DeviceModel(BaseModel):
    """One device in the registration. Both sections are mandatory objects."""

    Identifiers: IdentifiersModel = Field(..., description="**Mandatory.** What the product is.")
    Unique_Parameters: UniqueParametersModel = Field(..., description="**Mandatory.** Which unit this is.")


class RegisterRequest(BaseModel):
    """One registration covering a tenant, a locale, an optional customer, and one or more
    devices. The three string fields default to `""` in the schema but are **rejected with
    `400` when left blank** — treat them as mandatory."""

    clientkey: str = Field(
        "",
        description=(
            "**Required in practice.** Tenant key; must match a `ClientKey` record. Blank, "
            "`null` or the literal `\"string\"` are rejected with `400`."
        ),
        examples=["acme_uk_live"],
    )
    locale: str = Field(
        "",
        description=(
            "**Required in practice.** Locale code such as `en_GB`; must exist in "
            "`Locale_Params`. Rejected with `400` when blank or unsupported."
        ),
        examples=["en_GB"],
    )
    source: str = Field(
        "",
        description="**Required in practice.** Origin of the registration, e.g. `web`, `kiosk`.",
        examples=["web"],
    )
    customer: Optional[Customer] = Field(
        None,
        description="Optional end-customer details and contact consents.",
    )
    Devices: List[DeviceModel] = Field(..., description="**Mandatory.** The devices being registered.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "clientkey": "acme_uk_live",
                "locale": "en_GB",
                "source": "web",
                "customer": {
                    "name": "Jane Okafor",
                    "email": "jane.okafor@example.com",
                    "phone": "+447700900123",
                    "Opt_SMS": True,
                    "Opt_email": True,
                },
                "Devices": [
                    {
                        "Identifiers": {"GTIN": "5011773057240", "make": "Bosch", "model": "SMS6ZCI00G"},
                        "Unique_Parameters": {
                            "serial": "SN-8841203",
                            "purchase_date": "2025-05-01",
                            "price": 449.99,
                            "client_ref": "ORD-2026-00918",
                        },
                    }
                ],
            }
        }
    }

# ---------- Helper Functions ----------

def generate_activation_code(length=6):
    """Generate a random activation code of uppercase letters and digits."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def generate_qr_code(url):
    """Generate a QR code image (base64-encoded PNG) from a URL."""
    qr = qrcode.make(url)
    buffer = io.BytesIO()
    qr.save(buffer, format="PNG")
    buffer.seek(0)
    img_str = base64.b64encode(buffer.getvalue()).decode()
    return img_str

def prepare_doc_for_embed(doc):
    """Convert MongoDB _id to string for embedding in responses."""
    if not doc:
        return None
    new_doc = dict(doc)
    if '_id' in new_doc:
        new_doc['_id'] = str(new_doc['_id'])
    if 'Identifiers' in new_doc and isinstance(new_doc['Identifiers'], dict):
        if '_id' in new_doc['Identifiers']:
            new_doc['Identifiers']['_id'] = str(new_doc['Identifiers']['_id'])
    return new_doc

def extract_locale_specific_data(doc, locale):
    """Return the first Locale_Specific_Data dict matching the locale."""
    if not doc or "Locale_Specific_Data" not in doc:
        return None
    lsd = doc["Locale_Specific_Data"]
    entry = next((item for item in lsd if item.get("locale") == locale), None)
    return entry

def valid_value(val):
    """Return True if the value is non-empty, not 'string', not None."""
    return val is not None and str(val).strip() != "" and str(val).strip().lower() != "string"

def validate_purchase_date(date_str):
    """Validate the date string is in YYYY-MM-DD format."""
    if not date_str:
        return True
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False

def validate_mandatory_fields(payload):
    """Check for required root fields (clientkey, locale, source)."""
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
    """
    Try to find a matching CustomSKU document for this device's identifiers.
    Follows priority: id -> SKU -> GTIN.
    Only returns the document if Locale_Specific_Data matches the locale.
    """
    customsku_doc = None
    # 1. By id
    if valid_value(ids.id):
        try:
            object_id = ObjectId(ids.id)
            customsku_doc = customsku_collection.find_one({
                "_id": object_id,
                "Client": client_id,
                "Locale_Specific_Data.locale": locale
            })
        except Exception:
            pass
    # 2. By SKU
    if not customsku_doc and valid_value(ids.SKU):
        customsku_doc = customsku_collection.find_one({
            "Identifiers.SKU": ids.SKU,
            "Client": client_id,
            "Locale_Specific_Data.locale": locale
        })
    # 3. By GTIN
    if not customsku_doc and valid_value(ids.GTIN):
        customsku_doc = customsku_collection.find_one({
            "Identifiers.GTIN": ids.GTIN,
            "Client": client_id,
            "Locale_Specific_Data.locale": locale
        })
    return customsku_doc

def lookup_mastersku(customsku_doc, locale):
    """
    If CustomSKU has a MasterSKU, try to find a MasterSKU document 
    with the correct _id and locale.
    """
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
        return mastersku_doc
    except Exception:
        return None

def fallback_value(input_val, *fallbacks):
    """
    Returns the first non-blank value among input_val, then fallbacks in order.
    Used for fallback field population.
    """
    def is_blank(val):
        return val is None or str(val).strip() == "" or str(val).strip().lower() == "string"
    if not is_blank(input_val):
        return input_val
    for f in fallbacks:
        if not is_blank(f):
            return f
    return input_val  # blank if nothing found

# ---------- The Endpoint ----------

@router.post(
    "/register",
    summary="Register devices and issue an activation code and QR",
    response_description="The registration id, its activation code and QR, and a per-device result.",
    responses=secured({
        200: json_response(
            "The registration was processed. `status` is `matched` when at least one device "
            "resolved to a SKU, `error logged` when none did.",
            {
                "status": "matched",
                "registration_id": "6820f1c9a4b21d0f8c9e4471",
                "activation_code": "7KQ2FB",
                "registration_url": "https://www.activlink.io/register?id=6820f1c9a4b21d0f8c9e4471",
                "registration_qr": "iVBORw0KGgoAAAANSUhEUgAA… (base64 PNG)",
                "devices": [
                    {
                        "device_id": "b6f2a0d4-9a1e-4a5f-9f0e-2c3d4e5f6a7b",
                        "Identifiers": {"GTIN": "5011773057240", "make": "Bosch", "model": "SMS6ZCI00G"},
                        "Unique_Parameters": {"serial": "SN-8841203", "price": 449.99},
                        "customSKU_id": "681aa2f1c4b21d0f8c9e0012",
                        "masterSKU_id": "681aa2f1c4b21d0f8c9e0044",
                        "status": "matched",
                        "registered_at": "2026-08-06T10:14:52.113000Z",
                    }
                ],
            },
        ),
        400: error(
            "`clientkey`, `locale` or `source` was blank or a placeholder, the client is "
            "unknown, or the locale is unsupported.",
            "Invalid clientkey.",
        ),
    }),
)
def register(payload: RegisterRequest, _: None = Depends(verify_token)):
    """
    Register devices from an embedded/widget journey and return everything needed to complete
    activation: a registration id, a short activation code, a landing URL, and a QR image.

    Compared with `POST /device-register`, this endpoint stores **one registration document
    covering the whole batch** (including the optional customer), and returns activation
    artefacts at the root rather than per device.

    - Each device is enriched from the CustomSKU/MasterSKU catalogue for the given client and
      locale; unmatched devices come back with `status: "error logged"`.
    - `registration_qr` is a **base64-encoded PNG** of `registration_url` — render it directly
      with `<img src="data:image/png;base64,…">`.
    - `activation_code` is a random six-character code, identical for every device in the batch.
    - If **no** device matched, the whole registration is written to `Registrations_Error_Log`
      instead of `Registrations` and the root `status` is `error logged`. The call still returns
      `200` with a usable registration id.

    `clientkey`, `locale` and `source` are mandatory in practice; `customer` is optional, but a
    non-blank `customer.email` must be a valid address.
    """
    # --- Root mandatory fields validation ---
    validate_mandatory_fields(payload)

    # --- Check clientkey and locale exist in system ---
    client_doc = clients_collection.find_one({"ClientKey": payload.clientkey})
    if not client_doc:
        raise HTTPException(status_code=400, detail="Invalid clientkey.")
    locale_doc = locale_params_collection.find_one({"locale": payload.locale})
    if not locale_doc:
        raise HTTPException(status_code=400, detail="Locale is not supported in system.")

    device_results = []
    any_matched = False

    for device in payload.Devices:
        ids = device.Identifiers
        unique = device.Unique_Parameters
        device_id = str(uuid.uuid4())

        # --- Validate purchase date format ---
        if unique.purchase_date and not validate_purchase_date(unique.purchase_date):
            device_results.append({
                "device_id": device_id,
                "status": "error logged",
                "detail": "Invalid purchase date format. Should be YYYY-MM-DD (e.g. 2025-05-01).",
                "Identifiers": ids.dict(),
                "Unique_Parameters": unique.dict(),
                "registered_at": datetime.utcnow().isoformat() + "Z"
            })
            continue

        # --- Identification logic: must have GTIN (not '', '0', or null), or make+model, or SKU ---
        identification_ok = False
        if valid_value(ids.GTIN) and str(ids.GTIN).strip() != "0":
            identification_ok = True
        elif valid_value(ids.make) and valid_value(ids.model):
            identification_ok = True
        elif valid_value(ids.SKU):
            identification_ok = True

        if not identification_ok:
            device_results.append({
                "device_id": device_id,
                "status": "error logged",
                "detail": "You must provide a valid GTIN (not '', null, or '0'), or valid Make AND Model (not '', 'string', or null), or valid SKU (not '', 'string', or null).",
                "Identifiers": ids.dict(),
                "Unique_Parameters": unique.dict(),
                "registered_at": datetime.utcnow().isoformat() + "Z"
            })
            continue

        # --- Lookup CustomSKU & MasterSKU, filter for correct locale ---
        customsku_doc = lookup_customsku(ids, client_doc["Client_ID"], payload.locale)
        customsku_id = str(customsku_doc["_id"]) if customsku_doc else None
        customsku_obj = prepare_doc_for_embed(customsku_doc)
        lsd = None
        if customsku_obj and "Locale_Specific_Data" in customsku_obj:
            lsd = extract_locale_specific_data(customsku_obj, payload.locale)
            customsku_obj["Locale_Specific_Data"] = [lsd] if lsd else []

        mastersku_doc = lookup_mastersku(customsku_doc, payload.locale)
        mastersku_id = str(mastersku_doc["_id"]) if mastersku_doc else None
        mastersku_obj = prepare_doc_for_embed(mastersku_doc)
        if mastersku_obj and "Locale_Specific_Data" in mastersku_obj:
            lsd_master = extract_locale_specific_data(mastersku_obj, payload.locale)
            mastersku_obj["Locale_Specific_Data"] = [lsd_master] if lsd_master else []

        # --- Fallback field population for blank fields ---
        fields_to_fill = [
            ("make", "Make"),
            ("model", "Model"),
            ("SKU", "SKU"),
            ("category", "Category"),
            ("gtee_parts", "gtee_parts"),
            ("gtee_labour", "gtee_labour"),
            ("promo", "promo"),
            ("price", "Price"),
        ]

        for field, sku_field in fields_to_fill:
            fallback_locale = lsd.get(sku_field) if lsd and lsd.get(sku_field) is not None else None

            # For guarantee/promo fields, also try Guarantees object in locale-specific data
            fallback_gtee = None
            if field in ("gtee_parts", "gtee_labour", "promo") and lsd and "Guarantees" in lsd:
                if field == "gtee_parts":
                    fallback_gtee = lsd["Guarantees"].get("Parts")
                elif field == "gtee_labour":
                    fallback_gtee = lsd["Guarantees"].get("Labour")
                elif field == "promo":
                    fallback_gtee = lsd["Guarantees"].get("Promotion")
            if field == "category":
                fallback_root = customsku_obj.get("Category") if customsku_obj and customsku_obj.get("Category") is not None else None
                value = fallback_value(getattr(ids, field, None), fallback_locale, fallback_root)
                setattr(ids, field, value)
            else:
                fallback_identifiers = None
                if customsku_obj and "Identifiers" in customsku_obj:
                    fallback_identifiers = (
                        customsku_obj["Identifiers"].get(field)
                        or customsku_obj["Identifiers"].get(field.capitalize())
                    )
                fallback_root = customsku_obj.get(sku_field) if customsku_obj and customsku_obj.get(sku_field) is not None else None
                if field == "price":
                    value = fallback_value(unique.price, fallback_locale, fallback_gtee, fallback_identifiers, fallback_root)
                    unique.price = value
                else:
                    value = fallback_value(getattr(ids, field, None), fallback_locale, fallback_gtee, fallback_identifiers, fallback_root)
                    setattr(ids, field, value)

        # --- title: ONLY fallback to locale-specific data Title if input is blank ---
        ids.title = ids.title or (lsd.get("Title") if lsd and lsd.get("Title") is not None else "")

        # --- price fallback for Unique_Parameters from CustomSKU.Locale_Specific_Data.MSRP ---
        if (unique.price in (0, None, "", "string")) and lsd and lsd.get("MSRP") is not None:
            try:
                unique.price = float(lsd.get("MSRP"))
            except Exception:
                unique.price = 0

        matched_status = "matched" if (customsku_obj or mastersku_obj) else "no match"
        if matched_status == "matched":
            any_matched = True

        device_results.append({
            "device_id": device_id,
            "Identifiers": ids.dict(),
            "Unique_Parameters": unique.dict(),
            "customSKU_id": customsku_id,
            "masterSKU_id": mastersku_id,
            "masterSKU": mastersku_obj,
            "status": "matched" if matched_status == "matched" else "error logged",
            "registered_at": datetime.utcnow().isoformat() + "Z"
        })

    # --- Build and insert the registration doc (root) ---
    registration_doc = {
        "clientkey": payload.clientkey,
        "locale": payload.locale,
        "source": payload.source,
        "customer": payload.customer.dict() if payload.customer else {},
        "devices": device_results,
        "status": "matched" if any_matched else "error logged",
        "registered_at": datetime.utcnow().isoformat() + "Z"
    }

    # --- Insert registration doc, get ObjectId ---
    if any_matched:
        result = registrations_collection.insert_one(registration_doc)
    else:
        result = registrations_error_log_collection.insert_one(registration_doc)
    registration_id = str(result.inserted_id)

    # --- Root activation code/URL/QR (same for all devices in registration) ---
    activation_code = generate_activation_code()
    registration_url = f"https://www.activlink.io/register?id={registration_id}"
    registration_qr = generate_qr_code(registration_url)

    # --- Update registration doc with activation fields (root) ---
    update_fields = {
        "Activation Code": activation_code,
        "registration_url": registration_url,
        "registration_qr": registration_qr
    }
    if any_matched:
        registrations_collection.update_one(
            {"_id": result.inserted_id},
            {"$set": update_fields}
        )
    else:
        registrations_error_log_collection.update_one(
            {"_id": result.inserted_id},
            {"$set": update_fields}
        )

    # --- Return response (activation fields at root, not per-device) ---
    return {
        "status": registration_doc["status"],
        "registration_id": registration_id,
        "activation_code": activation_code,
        "registration_url": registration_url,
        "registration_qr": registration_qr,
        "devices": device_results
    }
