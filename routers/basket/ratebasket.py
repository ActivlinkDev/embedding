from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional, Tuple
from pymongo import MongoClient
from bson import ObjectId
import os
from utils.dependencies import verify_token

router = APIRouter(tags=["Basket"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
basket_collection = db["Basket_Quotes"]
rules_collection = db["BundleDiscountRules"]


class RateBasketRequest(BaseModel):
    basket_id: str = Field(..., description="Basket_Quotes _id as string")


class RuleResult(BaseModel):
    rule_id: str
    name: str
    priority: int
    ruleType: str
    discount_pence: int
    explanation: Optional[str] = None


class RateBasketResponse(BaseModel):
    basket_id: str
    subtotal_pence: int
    eligible_rules: List[RuleResult]
    best: Optional[RuleResult] = None
    final_total_pence: int


# ---- helpers ----

def _as_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _price_pence(item: Dict[str, Any]) -> int:
    if "rounded_price_pence" in item and item["rounded_price_pence"] is not None:
        return _as_int(item["rounded_price_pence"], 0)
    if "rounded_price" in item and item["rounded_price"] is not None:
        try:
            return int(round(float(item["rounded_price"]) * 100))
        except Exception:
            return 0
    return 0


def _match_applies_to(rule: Dict[str, Any], item: Dict[str, Any]) -> bool:
    applies = rule.get("appliesTo", {}) or {}

    def in_list_or_empty(val: Optional[str], arr: List[str], transform=None) -> bool:
        if not arr:
            return True
        if val is None:
            return False
        v = transform(val) if transform else val
        arr_t = [transform(x) if transform else x for x in arr]
        return v in arr_t

    currency_ok = in_list_or_empty(item.get("currency"), applies.get("currency", []), str.upper)
    locale_ok = in_list_or_empty(item.get("locale"), applies.get("locale", []))
    client_ok = in_list_or_empty(item.get("client"), applies.get("client", []), str.lower)

    product_ids = applies.get("productIds", []) or []
    product_ok = True if not product_ids else (item.get("product_id") in product_ids)

    # categoryGroups fallback: if provided, match against item.category directly
    cat_groups = applies.get("categoryGroups", []) or []
    category_ok = True if not cat_groups else (item.get("category") in cat_groups)

    mode_rule = applies.get("mode", "any")
    mode_ok = True if mode_rule in (None, "any") else (item.get("mode") == mode_rule)

    return currency_ok and locale_ok and client_ok and product_ok and category_ok and mode_ok


def _group_key(item: Dict[str, Any], constraints: Dict[str, Any]) -> Tuple:
    key = []
    if constraints.get("sameModeRequired"):
        key.append(item.get("mode"))
    if constraints.get("sameTermRequired"):
        key.append(item.get("poc"))
    if constraints.get("sameProductIdRequired"):
        key.append(item.get("product_id"))
    if constraints.get("sameCategoryRequired"):
        key.append(item.get("category"))
    return tuple(key) if key else ("ALL",)


def _apply_tiered_percent(rule: Dict[str, Any], items: List[Dict[str, Any]]) -> Tuple[int, str]:
    """Return (discount_pence, explanation)."""
    constraints = rule.get("constraints", {}) or {}
    params = rule.get("ruleParams", {}) or {}
    tiers = params.get("tiers", []) or []
    apply_base = params.get("applyBase", "subtotal")
    cap = _as_int(params.get("capAmountPence", 0), 0)

    # Group per constraints
    groups: Dict[Tuple, List[Dict[str, Any]]] = {}
    for it in items:
        k = _group_key(it, constraints)
        groups.setdefault(k, []).append(it)

    total_discount = 0
    parts = []

    # Sort tiers by minItems ascending
    tiers_sorted = sorted(tiers, key=lambda t: t.get("minItems", 0))

    for gkey, gitems in groups.items():
        count = len(gitems)
        # find highest eligible tier
        percent = 0
        for t in tiers_sorted:
            if count >= _as_int(t.get("minItems", 0), 0):
                percent = max(percent, _as_int(t.get("percentOff", 0), 0))
        if percent <= 0:
            continue
        if apply_base != "subtotal":
            # For now only subtotal is supported
            continue
        subtotal = sum(_price_pence(it) for it in gitems)
        d = int(subtotal * percent / 100)
        total_discount += d
        parts.append(f"{count} items in {gkey} -> {percent}% of {subtotal}p = {d}p")

    if cap > 0 and total_discount > cap:
        parts.append(f"cap {cap}p applied (was {total_discount}p)")
        total_discount = cap

    return total_discount, "; ".join(parts)


def _evaluate_rule(rule: Dict[str, Any], items: List[Dict[str, Any]]) -> RuleResult:
    # Filter items that match appliesTo
    matched = [it for it in items if _match_applies_to(rule, it)]
    discount = 0
    explanation = None
    rtype = rule.get("ruleType")

    if rtype == "TIERED_PERCENT":
        discount, explanation = _apply_tiered_percent(rule, matched)
    else:
        # Unknown rule: no discount
        discount = 0
        explanation = "Unsupported ruleType"

    return RuleResult(
        rule_id=str(rule.get("_id")),
        name=rule.get("name", ""),
        priority=int(rule.get("priority", 0)),
        ruleType=rtype or "",
        discount_pence=int(discount),
        explanation=explanation,
    )


@router.post("/basket/rate", response_model=RateBasketResponse)
def rate_basket(payload: RateBasketRequest, _: None = Depends(verify_token)):
    # Fetch basket
    try:
        bid = ObjectId(payload.basket_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    basket = basket_collection.find_one({"_id": bid})
    if not basket:
        raise HTTPException(status_code=404, detail="Basket not found")

    items: List[Dict[str, Any]] = basket.get("Basket", []) or []
    subtotal_pence = sum(_price_pence(it) for it in items)

    # Load active rules
    rules = list(rules_collection.find({"active": True}))

    # Evaluate all rules
    results = [_evaluate_rule(r, items) for r in rules]

    # Choose best rule by discount_pence then priority (higher priority wins if same discount)
    best: Optional[RuleResult] = None
    for r in sorted(results, key=lambda rr: (-rr.discount_pence, -rr.priority)):
        if r.discount_pence > 0:
            best = r
            break

    discount = best.discount_pence if best else 0
    final_total = max(0, subtotal_pence - discount)

    return RateBasketResponse(
        basket_id=str(basket["_id"]),
        subtotal_pence=int(subtotal_pence),
        eligible_rules=results,
        best=best,
        final_total_pence=int(final_total),
    )
