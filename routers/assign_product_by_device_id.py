from fastapi import APIRouter, HTTPException, Depends
from bson import ObjectId
from pymongo import MongoClient
import os
from datetime import datetime

from utils.dependencies import verify_token
from .product_assignment import product_assignment, ProductAssignmentRequest

router = APIRouter(tags=["Device Product Assignment"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
devices_collection = db["Devices"]

@router.get("/assign_product_for_device/{device_id}")
def assign_product_for_device(device_id: str, _: None = Depends(verify_token)):
    # 1. Lookup device by ObjectId
    try:
        obj_id = ObjectId(device_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid device_id format.")
    device = devices_collection.find_one({"_id": obj_id})
    if not device:
        raise HTTPException(status_code=404, detail="Device not found.")

    # 2. Extract required parameters
    try:
        client_ = device["client"]
        source = device["source"]
        identifiers = device.get("identifiers", {})
        category = identifiers.get("category") or ""
        price = device.get("registrationParameters", {}).get("price") or 0
        locale = device["locale"]
        purchase_date = device.get("registrationParameters", {}).get("purchaseDate")
        if not purchase_date:
            purchase_date = datetime.utcnow().strftime("%Y-%m-%d")
        gtee = (
            identifiers.get("gteeLabour")
            or identifiers.get("gteeParts")
            or 0
        )
        try:
            gtee = int(gtee)
        except Exception:
            gtee = 0
        currency = device.get("registrationParameters", {})["currency"]
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Device document missing required fields: {str(e)}")

    # 3. Build product assignment request payload
    req_payload = ProductAssignmentRequest(
        client=client_,
        source=source,
        category=category,
        price=price,
        locale=locale,
        purchase_date=purchase_date,
        gtee=gtee,
        currency=currency
    )

    # 4. Call the assignment logic
    assignment_result = product_assignment(req_payload)

    # 5. Flatten products into the new array format
    product_list = []
    for prod in assignment_result.get("products", []):
        product_id = prod["productId"]
        mode = prod["POC"]["mode"]
        for duration in prod["POC"]["durationMonths"]:
            product_entry = {
                "product_id": product_id,
                "currency": assignment_result["input"]["currency"],
                "locale": assignment_result["input"]["locale"],
                "poc": duration,
                "category": assignment_result["input"]["category"],
                "age": assignment_result["age_in_months"],
                "price": assignment_result["input"]["price"],
                "multi_count": 1,
                "client": assignment_result["input"]["client"],
                "source": assignment_result["input"]["source"],
                "mode": mode
            }
            product_list.append(product_entry)

    return {
        "Inputs": req_payload.dict(),
        "Products": product_list
    }
