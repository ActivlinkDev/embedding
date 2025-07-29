from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator, constr
from datetime import datetime
from utils.dependencies import verify_token
from pymongo import MongoClient
import os

router = APIRouter(tags=["Product Assignment"])

# MongoDB setup
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
product_assignments = db["ProductAssignment"]
error_log_collection = db["Error_Log_ProductAssignment"]

DEBUG = True  # Set to False to silence debug prints

# ---------------------- Helpers ------------------------

def debug_print(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

def calculate_age_in_months(purchase_date: str) -> int:
    purchase_dt = datetime.strptime(purchase_date, "%Y-%m-%d")
    now = datetime.utcnow()
    age_months = (now.year - purchase_dt.year) * 12 + (now.month - purchase_dt.month)
    if now.day < purchase_dt.day:
        age_months -= 1
    return max(age_months, 0)

def criteria_failure_reasons(crit, locale, gtee, age_in_months, price, currency):
    reasons = []
    if locale not in crit.get("locale", []):
        reasons.append(f"locale '{locale}' not in {crit.get('locale', [])}")
    if gtee not in crit.get("guaranteeDuration", []):
        reasons.append(f"gtee {gtee} not in {crit.get('guaranteeDuration', [])}")
    if not (crit.get("monthsLow", 0) <= age_in_months <= crit.get("monthsHigh", 9999)):
        reasons.append(f"age_in_months {age_in_months} not in [{crit.get('monthsLow', 0)}, {crit.get('monthsHigh', 9999)}]")
    if not (crit.get("msrpLow", 0) <= price <= crit.get("msrpHigh", 999999)):
        reasons.append(f"price {price} not in [{crit.get('msrpLow', 0)}, {crit.get('msrpHigh', 999999)}]")
    if currency not in crit.get("currency", []):
        reasons.append(f"currency '{currency}' not in {crit.get('currency', [])}")
    return reasons

def find_strict_assignment(payload, age_in_months):
    """
    Find a doc where at least one criteria matches all fields.
    Return (doc_id, age_in_months, products) or None.
    Also returns debug_failed reasons.
    """
    docs = product_assignments.find({
        "activeClient": {"$elemMatch": {"client": payload.client, "source": payload.source}},
        "categoryGroup": {"$in": [payload.category]}
    })
    debug_failed = []
    for doc in docs:
        doc_id = str(doc["_id"])
        debug_print(f"\n--- Checking document: {doc_id} ---")
        for idx, crit in enumerate(doc.get("criteria", [])):
            reasons = criteria_failure_reasons(
                crit,
                payload.locale,
                payload.gtee,
                age_in_months,
                payload.price,
                payload.currency
            )
            debug_print(f"Checking criteria block {idx}:")
            debug_print("Criteria block:", crit)
            debug_print("Failure reasons:", reasons)
            if not reasons:
                debug_print("MATCH FOUND in doc", doc_id, "criteria block", idx)
                return doc_id, crit.get("products", []), debug_failed
            else:
                debug_failed.append({
                    "doc_id": doc_id,
                    "criteria_index": idx,
                    "failure_reasons": reasons
                })
    return None, None, debug_failed

def log_and_raise_error(error_type, error_detail, payload, status=404):
    error_log_collection.insert_one({
        "input": payload.dict(),
        "error_type": error_type,
        "error_detail": error_detail,
        "created_at": datetime.utcnow()
    })
    debug_print(f"DEBUG: {error_type}: {error_detail}")
    raise HTTPException(status_code=status, detail=error_detail)

# -------------------- Pydantic Model ------------------------

class ProductAssignmentRequest(BaseModel):
    client: str = Field(..., example="")
    source: str = Field(..., example="")
    category: str = Field(..., example="")
    price: float = Field(..., example=0)
    locale: str = Field(..., example="")
    purchase_date: str = Field(..., example="")
    gtee: int = Field(..., example=0)
    currency: constr(strip_whitespace=True, min_length=3, max_length=3, pattern="^[A-Z]{3}$") = Field(..., example="GBP")

    @field_validator("purchase_date")
    def validate_purchase_date_format(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except Exception:
            raise ValueError("purchase_date must be in YYYY-MM-DD format")

    def missing_fields(self):
        missing = []
        for field in ["client", "source", "category", "locale", "purchase_date", "currency"]:
            value = getattr(self, field)
            if not isinstance(value, str) or value.strip() == "":
                missing.append(field)
        if self.price is None or (isinstance(self.price, (int, float)) and self.price == 0):
            missing.append("price")
        if self.gtee is None or (isinstance(self.gtee, int) and self.gtee == 0):
            missing.append("gtee")
        return missing

# ------------------------ Endpoint --------------------------

@router.post("/product_assignment")
def product_assignment(payload: ProductAssignmentRequest, _: None = Depends(verify_token)):
    # Validate required fields
    missing = payload.missing_fields()
    if missing:
        log_and_raise_error(
            "validation",
            f"Missing or blank required field(s): {', '.join(missing)}",
            payload,
            status=422
        )

    age_in_months = calculate_age_in_months(payload.purchase_date)
    debug_print("\n==== PRODUCT ASSIGNMENT DEBUG ====")
    debug_print("INPUT PAYLOAD:", payload.dict())
    debug_print("AGE IN MONTHS:", age_in_months)

    doc_id, products, debug_failed = find_strict_assignment(payload, age_in_months)
    if doc_id and products is not None:
        return {
            "input": payload.dict(),
            "doc_id": doc_id,
            "age_in_months": age_in_months,
            "products": products
        }
    else:
        error_detail = {
            "debug_failed": debug_failed,
            "error": "No criteria matched in any ProductAssignment document."
        }
        error_log_collection.insert_one({
            "input": payload.dict(),
            "error_type": "no_criteria_match",
            "error_detail": error_detail,
            "created_at": datetime.utcnow()
        })
        debug_print("DEBUG: No criteria matched for any doc/criteria block.")
        return {
            "input": payload.dict(),
            "products": [],
            "error": "No criteria matched in any ProductAssignment document.",
            "details": debug_failed
        }
