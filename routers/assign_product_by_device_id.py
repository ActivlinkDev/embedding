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
error_log_collection = db["Error_Log_ProductAssignment"]

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

    # 2. Extract required parameters with robust checks and logging
    try:
        client_ = device.get("client")
        print(f"DEBUG: Extracted client from device: {repr(client_)}")
        if not client_ or not client_.strip():
            print(f"ERROR: Device missing or blank client! Device: {device}")
            raise HTTPException(status_code=400, detail="Device 'client' is missing or blank.")

        source = device.get("source")
        if not source or not source.strip():
            raise HTTPException(status_code=400, detail="Device 'source' is missing or blank.")

        identifiers = device.get("identifiers", {})
        category = identifiers.get("category") or ""
        price = device.get("registrationParameters", {}).get("price") or 0
        locale = device.get("locale")
        if not locale or not locale.strip():
            raise HTTPException(status_code=400, detail="Device 'locale' is missing or blank.")

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

        currency = device.get("registrationParameters", {}).get("currency")
        if not currency or not currency.strip():
            raise HTTPException(status_code=400, detail="Device 'currency' is missing or blank.")

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

    # === Log error and raise if no products found, include parameter(s) likely causing it ===
    if not product_list:
        parameter_checks = []
        failing_params = []
        input_dict = req_payload.dict()
        check_params = ["client", "source", "category", "price", "locale", "gtee", "currency"]
        for key in check_params:
            test_input = input_dict.copy()
            if isinstance(test_input[key], (int, float)):
                test_input[key] = 0
            else:
                test_input[key] = ""
            try:
                test_payload = ProductAssignmentRequest(**test_input)
                test_result = product_assignment(test_payload)
                has_products = any(
                    prod.get("POC", {}).get("durationMonths")
                    for prod in test_result.get("products", [])
                )
                result = "products_found" if has_products else "no_products"
                parameter_checks.append({
                    "parameter": key,
                    "test_value": test_input[key],
                    "result": result,
                    "products": test_result.get("products", [])
                })
                if has_products:
                    failing_params.append(key)
            except Exception as e:
                parameter_checks.append({
                    "parameter": key,
                    "test_value": test_input[key],
                    "result": "validation_error",
                    "validation_error": str(e)
                })

        error_message = "No products found for this device."
        if failing_params:
            error_message += " Likely problematic parameter(s): " + ", ".join(failing_params)
        log_entry = {
            "device_id": device_id,
            "original_inputs": req_payload.dict(),
            "parameter_checks": parameter_checks,
            "timestamp": datetime.utcnow().isoformat(),
            "message": error_message,
            "failing_params": failing_params
        }
        error_log_collection.insert_one(log_entry)
        raise HTTPException(status_code=404, detail=error_message)

    return {
        "Inputs": req_payload.dict(),
        "Products": product_list
    }
