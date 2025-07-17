from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from pymongo import MongoClient
from utils.dependencies import verify_token
from datetime import datetime
import os
import re

router = APIRouter(tags=["Rate Request"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
ratings = db["Rating"]
error_log_collection = db["Error_Log_RateRequest"]

class RateRequest(BaseModel):
    product_id: str = Field(..., example="")
    currency: str = Field(..., example="")
    locale: str = Field(..., example="")
    poc: int = Field(..., example=0)
    category: str = Field(..., example="")
    age: int = Field(..., example=0)
    price: float = Field(..., example=0)
    multi_count: int = Field(..., example=0)

    def missing_fields(self):
        missing = []
        for field in self.model_fields:
            val = getattr(self, field)
            if val in ("", None) or (isinstance(val, (int, float)) and val == 0):
                missing.append(field)
        return missing

# Helper: Fuzzy-normalize strings for comparison
def normalize(s):
    return re.sub(r'\W+', '', (s or '')).strip().lower()

# Find the price factor from the priceFactor list
def find_price_factor(price_factor_list, price):
    for pf in price_factor_list:
        if pf["priceLow"] <= price <= pf["priceHigh"]:
            return pf["factor"]
    return None

# Helper: round up to the next .49 or .99, but keep as is if already .49 or .99
def round_price_49_99(value):
    """
    Round up to the nearest .49 or .99.
    If already at .49 or .99, leave as is.
    """
    cents = round(value % 1, 2)
    whole = int(value)
    # Already at .49 or .99: leave as is
    if abs(cents - 0.49) < 0.001 or abs(cents - 0.99) < 0.001:
        return round(value, 2)
    # Round up to .49
    if cents < 0.49:
        return round(whole + 0.49, 2)
    # Round up to .99
    elif cents < 0.99:
        return round(whole + 0.99, 2)
    # Just in case, for values >= x.99, round up to (whole + 1.49)
    else:
        return round(whole + 1.49, 2)

# Collect all failed reasons per field for doc match (fuzzy)
def match_with_reasons(doc, payload):
    reasons = []

    if doc.get("currency") != payload.currency:
        reasons.append(f"currency '{payload.currency}' not matched")
    if payload.product_id not in doc.get("productID", []):
        reasons.append(f"product_id '{payload.product_id}' not in productID")
    if not any(normalize(lf.get("locale", "")) == normalize(payload.locale) for lf in doc.get("localeFactor", [])):
        reasons.append(f"locale '{payload.locale}' not matched in localeFactor")
    if str(payload.poc) not in doc.get("pocFactor", {}):
        reasons.append(f"poc '{payload.poc}' not found in pocFactor")

    # CATEGORY DEBUGGING (comment out in production)
    # print("\n=== CATEGORY MATCH DEBUG ===")
    # print("PAYLOAD CATEGORY:", repr(payload.category))
    # for cf in doc.get("categoryFactor", []):
    #     print("DOC DEVICE:", repr(cf.get("device", "")))
    #     print("Normalized payload.category:", normalize(payload.category), "Normalized doc device:", normalize(cf.get("device", "")))
    # print("===========================")

    if not any(normalize(cf.get("device", "")) == normalize(payload.category) for cf in doc.get("categoryFactor", [])):
        reasons.append(f"category '{payload.category}' not matched in categoryFactor")
    if str(payload.age) not in doc.get("ageFactor", {}):
        reasons.append(f"age '{payload.age}' not found in ageFactor")
    price_match = False
    for pf in doc.get("priceFactor", []):
        if pf["priceLow"] <= payload.price <= pf["priceHigh"]:
            price_match = True
            break
    if not price_match:
        reasons.append(f"price '{payload.price}' not in any priceFactor range")
    if str(payload.multi_count) not in doc.get("multiFactor", {}):
        reasons.append(f"multi_count '{payload.multi_count}' not found in multiFactor")

    return len(reasons) == 0, reasons

@router.post("/rate_request")
def rate_request(payload: RateRequest, _: None = Depends(verify_token)):
    # Validate all fields present and not blank
    missing = payload.missing_fields()
    if missing:
        error = f"Missing or blank required field(s): {', '.join(missing)}"
        error_log_collection.insert_one({
            "input": payload.dict(),
            "error_type": "validation",
            "error_detail": error,
            "created_at": datetime.utcnow()
        })
        raise HTTPException(status_code=422, detail=error)

    failure_reasons = []
    matching_doc = None

    # Only filter by product_id and currency initially (so we can gather field errors)
    for doc in ratings.find({"currency": payload.currency, "productID": {"$in": [payload.product_id]}}):
        matched, reasons = match_with_reasons(doc, payload)
        if matched:
            matching_doc = doc
            break
        else:
            failure_reasons.append({
                "doc_id": str(doc["_id"]),
                "reasons": reasons
            })

    if not matching_doc:
        error = {
            "message": "No rating config found matching all input fields.",
            "details": failure_reasons
        }
        error_log_collection.insert_one({
            "input": payload.dict(),
            "error_type": "not_found",
            "error_detail": error,
            "created_at": datetime.utcnow()
        })
        raise HTTPException(status_code=404, detail=error)

    # All factors guaranteed to exist in this doc now:
    base_fee = matching_doc["baseFee"]
    locale_factor = next(
        (f["factor"] for f in matching_doc.get("localeFactor", [])
         if normalize(f["locale"]) == normalize(payload.locale)),
        None
    )
    poc_factor = matching_doc.get("pocFactor", {}).get(str(payload.poc))
    category_factor = next(
        (f["factor"] for f in matching_doc.get("categoryFactor", [])
         if normalize(f["device"]) == normalize(payload.category)),
        None
    )
    age_factor = matching_doc.get("ageFactor", {}).get(str(payload.age))
    price_factor = find_price_factor(matching_doc.get("priceFactor", []), payload.price)
    multi_factor = matching_doc.get("multiFactor", {}).get(str(payload.multi_count))

    # Calculate rate
    rate = round(base_fee * locale_factor * poc_factor * category_factor * age_factor * price_factor * multi_factor, 2)
    rounded_price = round_price_49_99(rate)

    return {
        "input": payload.dict(),
        "factors": {
            "base_fee": base_fee,
            "locale_factor": locale_factor,
            "poc_factor": poc_factor,
            "category_factor": category_factor,
            "age_factor": age_factor,
            "price_factor": price_factor,
            "multi_factor": multi_factor
        },
        "rate": rate,
        "rounded_price": rounded_price
    }
