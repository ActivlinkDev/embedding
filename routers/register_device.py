from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from typing import Optional
from utils.dependencies import verify_token
from pymongo import MongoClient
from bson import ObjectId
import os
from datetime import datetime
import random
import string
import qrcode
import io
import base64

router = APIRouter(
    tags=["Register"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
clients_collection = db["ClientKey"]
locale_params_collection = db["Locale_Params"]
customsku_collection = db["CustomSKU"]
mastersku_collection = db["MasterSKU"]
registrations_collection = db["Registrations"]

class Customer(BaseModel):
    Opt_SMS: Optional[bool]
    Opt_email: Optional[bool]
    name: Optional[str]
    email: Optional[EmailStr]

class RegisterRequest(BaseModel):
    clientkey: str
    locale: str
    source: str
    GTIN: Optional[str] = None
    MAC: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    imei: Optional[str] = None
    make: Optional[str] = None
    client_ref: Optional[str] = None
    phone: Optional[str] = None
    gtee_parts: Optional[str] = None
    gtee_labour: Optional[str] = None
    promo: Optional[str] = None
    SKU: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    purchase_date: Optional[str] = None
    price: Optional[float] = None
    id: Optional[str] = None
    customer: Optional[Customer] = None

def generate_activation_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def generate_qr_code(url):
    qr = qrcode.make(url)
    buffer = io.BytesIO()
    qr.save(buffer, format="PNG")
    buffer.seek(0)
    img_str = base64.b64encode(buffer.getvalue()).decode()
    return img_str

def prepare_doc_for_embed(doc):
    if not doc:
        return None
    new_doc = dict(doc)
    if '_id' in new_doc:
        new_doc['_id'] = str(new_doc['_id'])
    return new_doc

@router.post("/register")
def register(payload: RegisterRequest, _: None = Depends(verify_token)):
    # 1. Validate ClientKey
    client_doc = clients_collection.find_one({"ClientKey": payload.clientkey})
    if not client_doc:
        raise HTTPException(status_code=400, detail="Invalid clientkey.")

    # 2. Validate locale exists in Locale_Params
    locale_doc = locale_params_collection.find_one({"locale": payload.locale})
    if not locale_doc:
        raise HTTPException(status_code=400, detail="Locale is not supported in system.")

    # 3. Identification logic
    if not (
        (payload.GTIN and payload.GTIN.strip() != "")
        or (payload.make and payload.model)
        or payload.SKU
        or payload.id
    ):
        raise HTTPException(
            status_code=400,
            detail="Must provide either GTIN (not empty), or Make and Model, or SKU, or id."
        )

    # 4. Lookup CustomSKU (by id, SKU+Client, GTIN+Client), must have Locale_Specific_Data.locale == payload.locale
    customsku_doc = None
    customsku_id = None
    mastersku_doc = None
    mastersku_id = None

    if payload.id:
        try:
            object_id = ObjectId(payload.id)
            customsku_doc = customsku_collection.find_one({
                "_id": object_id,
                "Client": client_doc["Client_ID"],
                "Locale_Specific_Data.locale": payload.locale
            })
        except Exception:
            pass

    if not customsku_doc and payload.SKU:
        customsku_doc = customsku_collection.find_one({
            "Identifiers.SKU": payload.SKU,
            "Client": client_doc["Client_ID"],
            "Locale_Specific_Data.locale": payload.locale
        })

    if not customsku_doc and payload.GTIN:
        customsku_doc = customsku_collection.find_one({
            "Identifiers.GTIN": payload.GTIN,
            "Client": client_doc["Client_ID"],
            "Locale_Specific_Data.locale": payload.locale
        })

    if customsku_doc:
        customsku_id = str(customsku_doc["_id"])
        # 5. Lookup MasterSKU by MasterSKU id in customsku_doc, must have Locale_Specific_Data.locale == payload.locale
        if "MasterSKU" in customsku_doc:
            try:
                master_id = customsku_doc["MasterSKU"]
                if isinstance(master_id, str):
                    master_id = ObjectId(master_id)
                mastersku_doc = mastersku_collection.find_one({
                    "_id": master_id,
                    "Locale_Specific_Data.locale": payload.locale
                })
                if mastersku_doc:
                    mastersku_id = str(mastersku_doc["_id"])
            except Exception:
                pass

    customsku_obj = prepare_doc_for_embed(customsku_doc)
    mastersku_obj = prepare_doc_for_embed(mastersku_doc)

    matched_status = "matched" if (customsku_obj or mastersku_obj) else "no match"
    activation_code = generate_activation_code()

    registration_doc = payload.dict()
    registration_doc["customSKU_id"] = customsku_id
    registration_doc["masterSKU_id"] = mastersku_id
    registration_doc["customSKU"] = customsku_obj
    registration_doc["masterSKU"] = mastersku_obj
    registration_doc["status"] = matched_status
    registration_doc["registered_at"] = datetime.utcnow().isoformat() + "Z"
    registration_doc["Activation Code"] = activation_code

    result = registrations_collection.insert_one(registration_doc)
    registration_id = str(result.inserted_id)

    activation_url = f"https://www.activlink.io/register?id={registration_id}"
    qr_code_base64 = generate_qr_code(activation_url)

    registrations_collection.update_one(
        {"_id": result.inserted_id},
        {"$set": {"activation_qr": qr_code_base64, "activation_url": activation_url}}
    )

    return {
        "status": matched_status,
        "activation_code": activation_code,
        "registration_id": registration_id,
        "customSKU_id": customsku_id,
        "masterSKU_id": mastersku_id,
        "customSKU": customsku_obj,
        "masterSKU": mastersku_obj,
        "activation_url": activation_url,
        "activation_qr": qr_code_base64
    }
