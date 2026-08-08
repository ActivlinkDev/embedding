from fastapi import APIRouter, HTTPException, Depends
from bson import ObjectId
from pymongo import MongoClient
import os
from datetime import datetime

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from .product_assignment import product_assignment, ProductAssignmentRequest

router = APIRouter(tags=["Assignments"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
devices_collection = db["Devices"]
error_log_collection = db["Error_Log_ProductAssignment"]

@router.get(
    "/assign_product_for_device/{device_id}",
    summary="List the cover products available for a registered device",
    response_description="The inputs derived from the device and every product/term combination it qualifies for.",
    responses=secured({
        200: json_response(
            "One entry per product and cover term. Each entry is ready to be sent to "
            "`POST /rate_request` as-is.",
            {
                "Inputs": {
                    "client": "ACME-UK",
                    "source": "web",
                    "category": "Dishwasher",
                    "price": 449.99,
                    "locale": "en_GB",
                    "purchase_date": "2025-05-01",
                    "gtee": 12,
                    "currency": "GBP",
                },
                "Products": [
                    {
                        "product_id": "ACME-EW-STD",
                        "currency": "GBP",
                        "locale": "en_GB",
                        "poc": 24,
                        "category": "Dishwasher",
                        "age": 15,
                        "price": 449.99,
                        "multi_count": 1,
                        "client": "ACME-UK",
                        "source": "web",
                        "mode": "live",
                    },
                    {
                        "product_id": "ACME-EW-STD",
                        "currency": "GBP",
                        "locale": "en_GB",
                        "poc": 36,
                        "category": "Dishwasher",
                        "age": 15,
                        "price": 449.99,
                        "multi_count": 1,
                        "client": "ACME-UK",
                        "source": "web",
                        "mode": "live",
                    },
                ],
                "DistinctProductIds": ["ACME-EW-STD"],
            },
        ),
        400: error(
            "`device_id` is not a valid id, or the stored device is missing a field this "
            "endpoint needs (`client`, `source`, `locale` or `currency`).",
            "Device 'currency' is missing or blank.",
        ),
        404: error(
            "The device does not exist, or no product matches it. The 404 body carries "
            "`match_diagnostics` explaining which criteria failed, and the same detail is "
            "written to `Error_Log_ProductAssignment`.",
            {
                "message": "No products found for this device.",
                "device_id": "6820f1c9a4b21d0f8c9e4471",
                "original_inputs": {"category": "Dishwasher", "price": 449.99, "locale": "en_GB"},
                "match_diagnostics": {"category": "matched", "priceBand": "no band covers 449.99"},
                "assignment_details": [],
            },
        ),
        500: error("The device document is malformed in a way this endpoint cannot recover from.", "Device document missing required fields: ..."),
    }),
)
def assign_product_for_device(device_id: str, _: None = Depends(verify_token)):
    """
    Work out which cover products a **already-registered** device qualifies for.

    This is the convenience wrapper around `POST /product_assignment`: instead of assembling the
    assignment inputs yourself, pass a `device_id` and they are read off the stored device —
    `client`, `source`, `locale`, `currency`, category, price, purchase date, and the longer of
    the labour/parts guarantee.

    The result is **flattened**: the underlying assignment returns each product with a list of
    cover terms, and this endpoint expands that into one entry per `product_id` × `poc`
    (duration in months). Each entry is exactly the shape `POST /rate_request` expects, so the
    normal flow is *assign → rate → generate payment link*.

    Path parameter `device_id` is mandatory. Defaults applied while reading the device:
    a missing `purchaseDate` becomes today, and a missing/​unparseable guarantee becomes `0`.
    """
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

    # === Log error and raise if no products found, including subset-match diagnostics ===
    if not product_list:
        diagnostics = assignment_result.get("match_diagnostics")
        error_detail = {
            "message": "No products found for this device.",
            "device_id": device_id,
            "original_inputs": req_payload.dict(),
            "match_diagnostics": diagnostics,
            "assignment_details": assignment_result.get("details", []),
        }
        log_entry = {
            **error_detail,
            "timestamp": datetime.utcnow().isoformat(),
        }
        error_log_collection.insert_one(log_entry)
        raise HTTPException(status_code=404, detail=error_detail)

    # === Add distinct product IDs array to response ===
    distinct_product_ids = list({prod["product_id"] for prod in product_list})

    return {
        "Inputs": req_payload.dict(),
        "Products": product_list,
        "DistinctProductIds": distinct_product_ids
    }
