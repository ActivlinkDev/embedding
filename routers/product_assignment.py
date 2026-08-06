from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator, constr
from datetime import datetime
from itertools import combinations
from typing import Any, Dict, List, Set
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from pymongo import MongoClient
import os

router = APIRouter(tags=["Assignments"])

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

DOC_MATCH_FIELDS = ("client", "source", "category")
CRITERIA_MATCH_FIELDS = ("locale", "gtee", "age", "price", "currency")
DIAGNOSIS_FIELDS = DOC_MATCH_FIELDS + CRITERIA_MATCH_FIELDS

def _active_client_entries(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    active_client = doc.get("activeClient", [])
    if isinstance(active_client, dict):
        return [active_client]
    if isinstance(active_client, list):
        return [item for item in active_client if isinstance(item, dict)]
    return []

def _doc_level_match(doc: Dict[str, Any], payload, subset_fields: Set[str]) -> bool:
    active_client_entries = _active_client_entries(doc)
    if "client" in subset_fields and "source" in subset_fields:
        if not any(
            entry.get("client") == payload.client and entry.get("source") == payload.source
            for entry in active_client_entries
        ):
            return False
    elif "client" in subset_fields:
        if not any(entry.get("client") == payload.client for entry in active_client_entries):
            return False
    elif "source" in subset_fields:
        if not any(entry.get("source") == payload.source for entry in active_client_entries):
            return False

    if "category" in subset_fields:
        if payload.category not in doc.get("categoryGroup", []):
            return False

    return True

def _criteria_subset_match(crit: Dict[str, Any], payload, age_in_months: int, subset_fields: Set[str]) -> bool:
    if "locale" in subset_fields and payload.locale not in crit.get("locale", []):
        return False
    if "gtee" in subset_fields and payload.gtee not in crit.get("guaranteeDuration", []):
        return False
    if "age" in subset_fields and not (crit.get("monthsLow", 0) <= age_in_months <= crit.get("monthsHigh", 9999)):
        return False
    if "price" in subset_fields and not (crit.get("msrpLow", 0) <= payload.price <= crit.get("msrpHigh", 999999)):
        return False
    if "currency" in subset_fields and payload.currency not in crit.get("currency", []):
        return False
    return True

def _document_matches_subset(doc: Dict[str, Any], payload, age_in_months: int, subset_fields: Set[str]) -> bool:
    if not _doc_level_match(doc, payload, subset_fields):
        return False

    criteria_fields = subset_fields.intersection(CRITERIA_MATCH_FIELDS)
    if not criteria_fields:
        return True

    for crit in doc.get("criteria", []):
        if _criteria_subset_match(crit, payload, age_in_months, criteria_fields):
            return True
    return False

def build_match_diagnostics(payload, age_in_months: int) -> Dict[str, Any]:
    active_docs = list(product_assignments.find({"status": "active"}))
    subset_checks: List[Dict[str, Any]] = []
    first_unmatched_subset_size = None
    first_unmatched_subsets: List[List[str]] = []

    for size in range(1, len(DIAGNOSIS_FIELDS) + 1):
        checks_for_size: List[Dict[str, Any]] = []
        for field_combo in combinations(DIAGNOSIS_FIELDS, size):
            subset_fields = set(field_combo)
            match_count = 0
            for doc in active_docs:
                if _document_matches_subset(doc, payload, age_in_months, subset_fields):
                    match_count += 1

            checks_for_size.append({
                "fields": list(field_combo),
                "match_count": match_count
            })

        subset_checks.append({
            "subset_size": size,
            "checks": checks_for_size
        })

        zero_match_subsets = [check["fields"] for check in checks_for_size if check["match_count"] == 0]
        if zero_match_subsets:
            first_unmatched_subset_size = size
            first_unmatched_subsets = zero_match_subsets
            break

    return {
        "first_unmatched_subset_size": first_unmatched_subset_size,
        "first_unmatched_subsets": first_unmatched_subsets,
        "subset_checks": subset_checks
    }

def find_strict_assignment(payload, age_in_months):
    """
    Find a doc where at least one criteria matches all fields.
    Return (doc_id, products, debug_failed) or None.
    """
    docs = product_assignments.find({
        "activeClient": {"$elemMatch": {"client": payload.client, "source": payload.source}},
        "categoryGroup": {"$in": [payload.category]},
        "status": "active"   # <-- Only active assignments
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
    """The facts about a device that decide which cover products it qualifies for.

    **Every field is mandatory**, and beyond mere presence the endpoint rejects blank strings
    and — for `price` and `gtee` — the value `0`, with `422`.
    """

    client: str = Field(
        ...,
        description="**Mandatory.** `Client_ID` the assignment rules belong to (not the ClientKey).",
        examples=["ACME-UK"],
    )
    source: str = Field(
        ...,
        description="**Mandatory.** Sales channel the rules are defined for, e.g. `web`, `pos`.",
        examples=["web"],
    )
    category: str = Field(
        ...,
        description="**Mandatory.** Product category, matched against the rule document.",
        examples=["Dishwasher"],
    )
    price: float = Field(
        ...,
        description=(
            "**Mandatory and non-zero.** Purchase price, matched against each rule's "
            "`msrpLow`/`msrpHigh` band. `0` is rejected with `422`."
        ),
        examples=[449.99],
    )
    locale: str = Field(
        ...,
        description="**Mandatory.** Locale code; must appear in the rule's `locale` list.",
        examples=["en_GB"],
    )
    purchase_date: str = Field(
        ...,
        description=(
            "**Mandatory, `YYYY-MM-DD`.** Any other format is rejected with `422`. Used to "
            "derive `age_in_months`, which must fall inside the rule's `monthsLow`/`monthsHigh`."
        ),
        examples=["2025-05-01"],
    )
    gtee: int = Field(
        ...,
        description=(
            "**Mandatory and non-zero.** Manufacturer guarantee in months; must appear in the "
            "rule's `guaranteeDuration` list. `0` is rejected with `422`."
        ),
        examples=[12],
    )
    currency: constr(strip_whitespace=True, min_length=3, max_length=3, pattern="^[A-Z]{3}$") = Field(
        ...,
        description="**Mandatory.** Exactly three upper-case letters (ISO 4217), e.g. `GBP`.",
        examples=["GBP"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "client": "ACME-UK",
                "source": "web",
                "category": "Dishwasher",
                "price": 449.99,
                "locale": "en_GB",
                "purchase_date": "2025-05-01",
                "gtee": 12,
                "currency": "GBP",
            }
        }
    }

    @field_validator("purchase_date")
    def validate_purchase_date_format(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except Exception:
            raise ValueError("purchase_date must be in YYYY-MM-DD format")

    def missing_fields(self):
        missing = []
        print("DEBUG: missing_fields() running on:", self.dict())
        for field in ["client", "source", "category", "locale", "purchase_date", "currency"]:
            value = getattr(self, field)
            print(f"  Checking field '{field}': value={repr(value)}")
            if not isinstance(value, str) or value.strip() == "":
                missing.append(field)
        if self.price is None or (isinstance(self.price, (int, float)) and self.price == 0):
            missing.append("price")
        if self.gtee is None or (isinstance(self.gtee, int) and self.gtee == 0):
            missing.append("gtee")
        print(f"DEBUG: Fields considered missing: {missing}")
        return missing

# ------------------------ Endpoint --------------------------

@router.post(
    "/product_assignment",
    summary="Find the cover products a device qualifies for",
    response_description="The matched assignment document and its products, or an empty list plus diagnostics.",
    responses=secured({
        200: json_response(
            "The request was valid. `products` may still be empty — see `match_diagnostics` "
            "when it is.",
            {
                "input": {
                    "client": "ACME-UK",
                    "source": "web",
                    "category": "Dishwasher",
                    "price": 449.99,
                    "locale": "en_GB",
                    "purchase_date": "2025-05-01",
                    "gtee": 12,
                    "currency": "GBP",
                },
                "doc_id": "6712c0f1a4b21d0f8c9e0031",
                "age_in_months": 15,
                "products": [
                    {
                        "productId": "ACME-EW-STD",
                        "POC": {"mode": "live", "durationMonths": [24, 36]},
                    }
                ],
            },
        ),
        422: error(
            "A mandatory field is missing or blank, `price`/`gtee` is `0`, `purchase_date` is "
            "not `YYYY-MM-DD`, or `currency` is not three upper-case letters. The failure is "
            "also written to `Error_Log_ProductAssignment`.",
            "The following required field(s) are missing or blank: price, gtee",
        ),
    }),
)
def product_assignment(payload: ProductAssignmentRequest, _: None = Depends(verify_token)):
    """
    Match a device against the `ProductAssignment` rules and return the cover products it
    qualifies for.

    Matching happens in two stages:

    1. **Document level** — `client`, `source` and `category` must match an assignment document.
    2. **Criteria level** — within that document, a criteria block must accept the `locale`,
       the `gtee`, the derived `age_in_months`, the `price` band and the `currency`.

    `age_in_months` is calculated from `purchase_date` to today and returned so you can see what
    was actually matched against.

    **No match is not an error.** The response is still `200`, with `products: []` plus
    `match_diagnostics` — a per-field account of which criteria rejected the device (for example
    `price 449.99 not in [0, 300]`) — and `details` listing the near-miss combinations tried.
    The same diagnostics are logged to `Error_Log_ProductAssignment`. Only a malformed request
    returns `422`.

    If the device is already registered, prefer `GET /assign_product_for_device/{device_id}`,
    which fills these inputs in from the stored device.
    """
    # Validate required fields
    debug_print("\n==== PRODUCT ASSIGNMENT DEBUG ====")
    debug_print("INPUT PAYLOAD:", payload.dict())
    missing = payload.missing_fields()
    if missing:
        log_and_raise_error(
            "validation",
            f"The following required field(s) are missing or blank: {', '.join(missing)}",
            payload,
            status=422
        )

    age_in_months = calculate_age_in_months(payload.purchase_date)
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
        diagnostics = build_match_diagnostics(payload, age_in_months)
        error_detail = {
            "debug_failed": debug_failed,
            "match_diagnostics": diagnostics,
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
            "details": debug_failed,
            "match_diagnostics": diagnostics
        }
