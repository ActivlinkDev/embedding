from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from pymongo import MongoClient
from utils.dependencies import verify_token
from datetime import datetime
import os
import re
from typing import List, Optional

router = APIRouter(tags=["Rate Request"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
ratings = db["Rating"]
error_log_collection = db["Error_Log_RateRequest"]
stripe_payment_collection = db["Stripe_Price_ID"]
quotes_collection = db["Quotes"]
error_log_stripe_collection = db["Error_Log_Stripe"]

# --- Models ---

class RateRequest(BaseModel):
    product_id: str = Field(..., example="")
    currency: str = Field(..., example="")
    locale: str = Field(..., example="")
    poc: int = Field(..., example=0)
    category: str = Field(..., example="")
    age: int = Field(..., example=0)
    price: float = Field(..., example=0)
    multi_count: int = Field(..., example=0)
    client: str = Field(..., example="acme_corp")
    source: str = Field(..., example="web_app")
    mode: str = Field(..., example="live")

    def missing_fields(self):
        missing = []
        for field in self.model_fields:
            val = getattr(self, field)
            if field == "age":
                if val in ("", None):  # Only blank/None age is missing, 0 is valid
                    missing.append(field)
            else:
                if val in ("", None) or (isinstance(val, (int, float)) and val == 0):
                    missing.append(field)
        return missing

class RateRequestBatch(BaseModel):
    deviceId: Optional[str] = Field(None, example="abc-123")
    requests: List[RateRequest]

# --- Utilities (Unchanged) ---

def normalize(s):
    return re.sub(r'\W+', '', (s or '')).strip().lower()

def find_price_factor(price_factor_list, price):
    for pf in price_factor_list:
        if pf["priceLow"] <= price <= pf["priceHigh"]:
            return pf["factor"]
    return None

def round_price_49_99(value):
    cents = round(value % 1, 2)
    whole = int(value)
    if abs(cents - 0.49) < 0.001 or abs(cents - 0.99) < 0.001:
        return round(value, 2)
    if cents < 0.49:
        return round(whole + 0.49, 2)
    elif cents < 0.99:
        return round(whole + 0.99, 2)
    else:
        return round(whole + 1.49, 2)

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

# --- Endpoint ---

@router.post("/rate_request")
def rate_request(
    payload: RateRequestBatch,
    _: None = Depends(verify_token)
):
    enriched_results = []
    device_id = payload.deviceId

    for req in payload.requests:
        enriched = req.dict()
        try:
            # Validate all fields present and not blank (age=0 is now valid)
            missing = req.missing_fields()
            if missing:
                error = f"Missing or blank required field(s): {', '.join(missing)}"
                error_log_collection.insert_one({
                    "input": req.dict(),
                    "error_type": "validation",
                    "error_detail": error,
                    "created_at": datetime.utcnow()
                })
                enriched["status"] = "error"
                enriched["error"] = error
                enriched_results.append(enriched)
                continue

            failure_reasons = []
            matching_doc = None

            # Only filter by product_id and currency initially (so we can gather field errors)
            for doc in ratings.find({"currency": req.currency, "productID": {"$in": [req.product_id]}}):
                matched, reasons = match_with_reasons(doc, req)
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
                    "input": req.dict(),
                    "error_type": "not_found",
                    "error_detail": error,
                    "created_at": datetime.utcnow()
                })
                enriched["status"] = "error"
                enriched["error"] = error
                enriched_results.append(enriched)
                continue

            base_fee = matching_doc["baseFee"]
            locale_factor = next(
                (f["factor"] for f in matching_doc.get("localeFactor", [])
                 if normalize(f["locale"]) == normalize(req.locale)),
                None
            )
            poc_factor = matching_doc.get("pocFactor", {}).get(str(req.poc))
            category_factor = next(
                (f["factor"] for f in matching_doc.get("categoryFactor", [])
                 if normalize(f["device"]) == normalize(req.category)),
                None
            )
            age_factor = matching_doc.get("ageFactor", {}).get(str(req.age))
            price_factor = find_price_factor(matching_doc.get("priceFactor", []), req.price)
            multi_factor = matching_doc.get("multiFactor", {}).get(str(req.multi_count))

            rate = round(base_fee * locale_factor * poc_factor * category_factor * age_factor * price_factor * multi_factor, 2)
            rounded_price = round_price_49_99(rate)

            mode_map = {
                "subscription": "recurring",
                "payment": "one_time"
            }
            price_type = mode_map.get(req.mode.lower(), "one_time")
            lookup_unit_amount = int(round(rounded_price * 100))

            stripe_query = {
                "currency": req.currency.lower(),
                "unit_amount": lookup_unit_amount,
                "type": price_type,
                "active": True
            }

            stripe_price_doc = stripe_payment_collection.find_one(stripe_query)
            stripe_price_id = stripe_price_doc["id"] if stripe_price_doc else None
            stripe_price_doc_filtered = {k: v for k, v in stripe_price_doc.items() if k != "_id"} if stripe_price_doc else None

            enriched["status"] = "ok"
            enriched["factors"] = {
                "base_fee": base_fee,
                "locale_factor": locale_factor,
                "poc_factor": poc_factor,
                "category_factor": category_factor,
                "age_factor": age_factor,
                "price_factor": price_factor,
                "multi_factor": multi_factor
            }
            enriched["rate"] = rate
            enriched["rounded_price"] = rounded_price
            enriched["stripe_price_lookup"] = {
                "query": stripe_query,
                "found": bool(stripe_price_doc),
                "stripe_price_id": stripe_price_id,
                "stripe_full_doc": stripe_price_doc_filtered
            }

            # --- Stripe error log if not found ---
            if (
                "stripe_price_lookup" in enriched
                and not enriched["stripe_price_lookup"].get("found", True)
            ):
                error_log_stripe_collection.insert_one({
                    "request": req.dict(),
                    "stripe_query": enriched["stripe_price_lookup"].get("query"),
                    "error_type": "stripe_price_not_found",
                    "error_detail": "Stripe price not found for this combination",
                    "created_at": datetime.utcnow()
                })

        except Exception as e:
            enriched["status"] = "error"
            enriched["error"] = str(e)

        enriched_results.append(enriched)

    # Only responses, deviceId, and created_at are stored in the Quotes collection
    quotes_collection.insert_one({
        "deviceId": device_id,
        "responses": enriched_results,
        "created_at": datetime.utcnow()
    })

    return enriched_results
