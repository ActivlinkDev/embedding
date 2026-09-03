from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator, constr
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from utils.api_docs import error, json_response, secured
from utils.category_tree import resolve as resolve_category
from utils.dependencies import verify_token
from pymongo import MongoClient
import logging
import os

router = APIRouter(tags=["Assignments"])

logger = logging.getLogger(__name__)

# MongoDB setup
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
product_assignments = db["ProductAssignment"]
error_log_collection = db["Error_Log_ProductAssignment"]

# Payloads carry customer-identifying data, so per-request tracing is opt-in.
DEBUG = os.getenv("PRODUCT_ASSIGNMENT_DEBUG", "").lower() in ("1", "true", "yes")

# The near-miss report walks every combination of the match fields against every
# active rule. That is useful when a rule is being written and far too expensive
# to run on the customer's path, so it is opt-in too.
MATCH_DIAGNOSTICS = os.getenv("PRODUCT_ASSIGNMENT_DIAGNOSTICS", "").lower() in ("1", "true", "yes")

# How precisely a rule named the device's category. A rule naming the category
# itself beats one naming its group, which beats one naming its sector — so
# "Home Appliances at 1.0, except Kettles at 0.75" needs no exclusion list.
SPECIFICITY = {"category": 3, "group": 2, "sector": 1}


_indexes_ready = False


def _ensure_indexes() -> None:
    """Index the fields the assignment query filters on.

    Called from the query path rather than at import: creating an index needs a
    live connection, and a module-level call blocks startup for the whole server
    selection timeout when Mongo is slow to answer.
    """
    global _indexes_ready
    if _indexes_ready:
        return
    _indexes_ready = True
    try:
        product_assignments.create_index(
            [("status", 1), ("who.client", 1), ("who.source", 1)],
            name="assignment_lookup",
        )
    except Exception:
        logger.exception("[assignment] could not create the lookup index")

# ---------------------- Helpers ------------------------

def debug_print(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

def calculate_age_in_months(purchase_date: str) -> int:
    purchase_dt = datetime.strptime(purchase_date, "%Y-%m-%d")
    now = datetime.now(timezone.utc)
    age_months = (now.year - purchase_dt.year) * 12 + (now.month - purchase_dt.month)
    if now.day < purchase_dt.day:
        age_months -= 1
    return max(age_months, 0)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_in_effect(rule: Dict[str, Any], today: Optional[str] = None) -> bool:
    """True when today falls inside the rule's validFrom/validTo window.

    Both bounds are optional `YYYY-MM-DD` strings; absent means unbounded, which
    is how every rule reads until someone schedules a change.
    """
    today = today or _today()
    valid_from = rule.get("validFrom")
    valid_to = rule.get("validTo")
    if valid_from and today < valid_from:
        return False
    if valid_to and today > valid_to:
        return False
    return True


def _what_match(rule: Dict[str, Any], placement: Dict[str, Optional[str]]) -> Optional[int]:
    """How precisely `rule.what` names this device's category, or None if it doesn't.

    Each level is tested only when the rule lists values for it, so an empty
    list reads as "don't test this level" rather than "match nothing". A rule
    matches if *any* level hits, and scores as the most precise level that hit.
    """
    what = rule.get("what") or {}
    best: Optional[int] = None
    tested_any = False

    for level in ("category", "group", "sector"):
        values = what.get(level) or []
        if not values:
            continue
        tested_any = True
        mine = placement.get(level)
        if mine and mine in values:
            score = SPECIFICITY[level]
            if best is None or score > best:
                best = score

    if not tested_any:
        # No level constrains anything. Almost certainly a mistake rather than a
        # deliberate "cover everything", so say so — but honour it.
        logger.warning(
            "[assignment] rule %s has no category, group or sector in `what`; "
            "it places no category restriction",
            rule.get("ruleId") or rule.get("_id"),
        )
        return SPECIFICITY["sector"]

    return best


def _in_range(value, bounds: Dict[str, Any], low_key: str = "min", high_key: str = "max",
              default_low=0, default_high=float("inf")) -> bool:
    low = bounds.get(low_key, default_low)
    high = bounds.get(high_key, default_high)
    if low is None:
        low = default_low
    if high is None:
        high = default_high
    return low <= value <= high


def when_failure_reasons(rule: Dict[str, Any], payload, age_in_months: int) -> List[str]:
    """Why this rule's `when` block rejected the device. Empty means it accepted.

    A list left empty means "any", so it is not tested.
    """
    when = rule.get("when") or {}
    reasons = []

    locales = when.get("locale") or []
    if locales and payload.locale not in locales:
        reasons.append(f"locale '{payload.locale}' not in {locales}")

    currencies = when.get("currency") or []
    if currencies and payload.currency not in currencies:
        reasons.append(f"currency '{payload.currency}' not in {currencies}")

    gtees = when.get("guaranteeMonths") or []
    if gtees and payload.gtee not in gtees:
        reasons.append(f"gtee {payload.gtee} not in {gtees}")

    age = when.get("deviceAgeMonths") or {}
    if not _in_range(age_in_months, age):
        reasons.append(
            f"age_in_months {age_in_months} not in "
            f"[{age.get('min', 0)}, {age.get('max', 'any')}]"
        )

    price = when.get("price") or {}
    if not _in_range(payload.price, price):
        reasons.append(
            f"price {payload.price} not in "
            f"[{price.get('min', 0)}, {price.get('max', 'any')}]"
        )

    return reasons


def _candidate_rules(payload) -> List[Dict[str, Any]]:
    """Active rules scoped to this client and channel."""
    _ensure_indexes()
    return list(product_assignments.find({
        "status": "active",
        "who": {"$elemMatch": {"client": payload.client, "source": payload.source}},
    }))


def find_matching_rule(payload, age_in_months: int) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """The rule that wins for this device, plus why the others lost.

    Every rule the client/channel query returned is evaluated, then the winners
    are ordered by priority, then by how precisely they named the category, then
    by `_id`. Ordering is explicit rather than whichever document Mongo happened
    to return first.
    """
    placement = resolve_category(payload.category)
    debug_print("CATEGORY PLACEMENT:", placement)

    today = _today()
    matches: List[Tuple[int, int, str, Dict[str, Any]]] = []
    rejected: List[Dict[str, Any]] = []

    for rule in _candidate_rules(payload):
        rule_id = rule.get("ruleId") or str(rule.get("_id"))
        reasons: List[str] = []

        if not _is_in_effect(rule, today):
            reasons.append(
                f"not in effect on {today} "
                f"(validFrom={rule.get('validFrom')}, validTo={rule.get('validTo')})"
            )

        specificity = _what_match(rule, placement)
        if specificity is None:
            what = rule.get("what") or {}
            reasons.append(
                f"category '{payload.category}' (group={placement.get('group')}, "
                f"sector={placement.get('sector')}) not named by what "
                f"(category={what.get('category') or []}, group={what.get('group') or []}, "
                f"sector={what.get('sector') or []})"
            )

        reasons.extend(when_failure_reasons(rule, payload, age_in_months))

        if reasons:
            debug_print(f"--- rule {rule_id} rejected: {reasons}")
            rejected.append({"rule_id": rule_id, "failure_reasons": reasons})
            continue

        debug_print(f"--- rule {rule_id} matched (specificity {specificity})")
        matches.append((int(rule.get("priority", 0)), specificity, str(rule.get("_id")), rule))

    if not matches:
        return None, rejected

    # Highest priority first, then most specific, then a stable id tiebreak.
    matches.sort(key=lambda m: (-m[0], -m[1], m[2]))

    if len(matches) > 1 and matches[0][:2] == matches[1][:2]:
        logger.warning(
            "[assignment] rules %s and %s tie on priority %s and specificity %s for "
            "client=%s source=%s category=%s; resolving by _id",
            matches[0][3].get("ruleId"), matches[1][3].get("ruleId"),
            matches[0][0], matches[0][1],
            payload.client, payload.source, payload.category,
        )

    return matches[0][3], rejected


def build_match_diagnostics(payload, age_in_months: int) -> Dict[str, Any]:
    """Which single condition, relaxed on its own, would have let a rule through.

    Cheaper and more direct than the old powerset sweep: for each rule that
    failed on exactly one condition, report that condition.
    """
    placement = resolve_category(payload.category)
    near_misses = []
    for rule in _candidate_rules(payload):
        reasons: List[str] = []
        if _what_match(rule, placement) is None:
            reasons.append("category")
        reasons.extend(when_failure_reasons(rule, payload, age_in_months))
        if len(reasons) == 1:
            near_misses.append({
                "rule_id": rule.get("ruleId") or str(rule.get("_id")),
                "only_blocker": reasons[0],
            })
    return {
        "category_placement": placement,
        "single_condition_blockers": near_misses,
    }


def log_and_raise_error(error_type, error_detail, payload, status=404):
    error_log_collection.insert_one({
        "input": payload.model_dump(),
        "error_type": error_type,
        "error_detail": error_detail,
        "created_at": datetime.now(timezone.utc),
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
        examples=["AO"],
    )
    source: str = Field(
        ...,
        description="**Mandatory.** Sales channel the rules are defined for, e.g. `POS`, `PON`.",
        examples=["POS"],
    )
    category: str = Field(
        ...,
        description=(
            "**Mandatory.** Device category. Resolved against the `Category` taxonomy so a rule "
            "can match it by category, by its group, or by its sector."
        ),
        examples=["Dishwasher"],
    )
    price: float = Field(
        ...,
        description=(
            "**Mandatory and non-zero.** Purchase price, matched against each rule's "
            "`when.price` band. `0` is rejected with `422`."
        ),
        examples=[449.99],
    )
    locale: str = Field(
        ...,
        description="**Mandatory.** Locale code; must appear in the rule's `when.locale` list.",
        examples=["en_GB"],
    )
    purchase_date: str = Field(
        ...,
        description=(
            "**Mandatory, `YYYY-MM-DD`.** Any other format is rejected with `422`. Used to "
            "derive `age_in_months`, which must fall inside the rule's `when.deviceAgeMonths`."
        ),
        examples=["2025-05-01"],
    )
    gtee: int = Field(
        ...,
        description=(
            "**Mandatory and non-zero.** Manufacturer guarantee in months; must appear in the "
            "rule's `when.guaranteeMonths` list. `0` is rejected with `422`."
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
                "client": "AO",
                "source": "POS",
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
        for field in ["client", "source", "category", "locale", "purchase_date", "currency"]:
            value = getattr(self, field)
            if not isinstance(value, str) or value.strip() == "":
                missing.append(field)
        if self.price is None or (isinstance(self.price, (int, float)) and self.price == 0):
            missing.append("price")
        if self.gtee is None or (isinstance(self.gtee, int) and self.gtee == 0):
            missing.append("gtee")
        return missing


# ------------------------ Service --------------------------

def assign_products(payload: ProductAssignmentRequest) -> Dict[str, Any]:
    """Match a device against the assignment rules.

    This is the in-process entry point. `/assign_product_for_device/{id}` and the
    widget quote endpoints call it directly, having already authenticated their
    own request — the HTTP endpoint below is a thin authenticated wrapper.
    """
    debug_print("\n==== PRODUCT ASSIGNMENT ====")
    debug_print("INPUT PAYLOAD:", payload.model_dump())

    missing = payload.missing_fields()
    if missing:
        log_and_raise_error(
            "validation",
            f"The following required field(s) are missing or blank: {', '.join(missing)}",
            payload,
            status=422,
        )

    age_in_months = calculate_age_in_months(payload.purchase_date)
    debug_print("AGE IN MONTHS:", age_in_months)

    rule, rejected = find_matching_rule(payload, age_in_months)

    if rule is not None:
        return {
            "input": payload.model_dump(),
            "doc_id": str(rule.get("_id")),
            "rule_id": rule.get("ruleId"),
            "age_in_months": age_in_months,
            "products": (rule.get("then") or {}).get("products", []),
        }

    error_detail = {
        "debug_failed": rejected,
        "error": "No assignment rule matched.",
    }
    if MATCH_DIAGNOSTICS:
        error_detail["match_diagnostics"] = build_match_diagnostics(payload, age_in_months)

    error_log_collection.insert_one({
        "input": payload.model_dump(),
        "error_type": "no_rule_match",
        "error_detail": error_detail,
        "created_at": datetime.now(timezone.utc),
    })
    debug_print("DEBUG: no rule matched.")

    result = {
        "input": payload.model_dump(),
        "products": [],
        "error": "No assignment rule matched.",
        "details": rejected,
    }
    if MATCH_DIAGNOSTICS:
        result["match_diagnostics"] = error_detail["match_diagnostics"]
    return result


# ------------------------ Endpoint --------------------------

@router.post(
    "/product_assignment",
    summary="Find the cover products a device qualifies for",
    response_description="The matched rule and its products, or an empty list plus the reasons.",
    responses=secured({
        200: json_response(
            "The request was valid. `products` may still be empty — see `details` when it is.",
            {
                "input": {
                    "client": "AO",
                    "source": "POS",
                    "category": "Dishwasher",
                    "price": 449.99,
                    "locale": "en_GB",
                    "purchase_date": "2025-05-01",
                    "gtee": 12,
                    "currency": "GBP",
                },
                "doc_id": "681bd53fad4ba559bc92f41b",
                "rule_id": "GBP-POS-EX1-WF1-150PLUS",
                "age_in_months": 15,
                "products": [
                    {"productId": "EX1", "mode": "payment", "terms": [12, 24, 36]},
                    {"productId": "WF1", "mode": "subscription", "terms": [1]},
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

    A rule is considered when it is `active`, in effect today, and its `who` list carries
    the device's `client` and `source`. It matches when:

    1. **`what`** names the device's category — at `category`, `group` or `sector` level.
       The category is resolved through the `Category` taxonomy, so a rule covering the
       sector *Home Appliances* also covers a Dishwasher without naming it. A level with an
       empty list is not tested.
    2. **`when`** accepts the `locale`, the `currency`, the `gtee`, the derived
       `age_in_months` and the `price`. An empty list means "any".

    When several rules match, the winner is the one with the highest `priority`; ties go to
    the rule that named the category most precisely (category beats group beats sector), and
    any remaining tie is broken by `_id`. That makes exceptions easy to write: a broad rule
    on a sector plus a narrow rule on one category, and the narrow one wins for that
    category without the broad one needing an exclusion list.

    `age_in_months` is calculated from `purchase_date` to today and returned so you can see
    what was actually matched against.

    **No match is not an error.** The response is still `200`, with `products: []` and
    `details` listing why each candidate rule was rejected (for example
    `price 449.99 not in [0, 300]`). The same detail is logged to
    `Error_Log_ProductAssignment`. Only a malformed request returns `422`.

    If the device is already registered, prefer `GET /assign_product_for_device/{device_id}`,
    which fills these inputs in from the stored device.
    """
    return assign_products(payload)
