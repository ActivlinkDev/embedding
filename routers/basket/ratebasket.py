from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional, Tuple
from pymongo import MongoClient
from bson import ObjectId
import os
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

router = APIRouter(tags=["Basket"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
basket_collection = db["Basket_Quotes"]
rules_collection = db["BundleDiscountRules"]


class RateBasketRequest(BaseModel):
    """The basket to price."""

    basket_id: str = Field(
        ...,
        description="**Mandatory.** The basket's `_id`, as returned by `POST /basket/add`.",
        examples=["68b2d1f0a4b21d0f8c9e8801"],
    )

    model_config = {"json_schema_extra": {"example": {"basket_id": "68b2d1f0a4b21d0f8c9e8801"}}}


class RuleResult(BaseModel):
    """One discount rule evaluated against the basket."""

    rule_id: str = Field(..., description="Id of the rule that was evaluated.", examples=["68c0a1b2c3d4e5f6a7b8c9d0"])
    name: str = Field(..., description="Human-readable rule name, suitable for showing a customer.", examples=["Multi-device 10% off"])
    priority: int = Field(
        ...,
        description="Tie-breaker when two rules offer the same discount — the higher priority wins.",
        examples=[10],
    )
    ruleType: str = Field(..., description="The kind of rule, e.g. `percentage`, `fixed`.", examples=["percentage"])
    discount: int = Field(
        ...,
        description="Discount **in minor units** (pence/cents). `0` means the rule did not apply.",
        examples=[715],
    )
    explanation: Optional[str] = Field(
        None,
        description="Why the rule did or did not apply.",
        examples=["2 qualifying devices — 10% off"],
    )


class RateBasketResponse(BaseModel):
    """The basket's totals and the discount rules considered. All amounts are in minor units."""

    basket_id: str = Field(..., examples=["68b2d1f0a4b21d0f8c9e8801"])
    subtotal: int = Field(..., description="Sum of every line before discount, in minor units.", examples=[14298])
    eligible_rules: List[RuleResult] = Field(
        ...,
        description="Every active rule evaluated, including those that produced no discount.",
    )
    best: Optional[RuleResult] = Field(
        None,
        description="The single rule applied. `null` when no rule produced a discount — **rules do not stack**.",
    )
    final_total: int = Field(..., description="`subtotal` minus the best discount, never below `0`.", examples=[13583])


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
        parts.append(f"{count} items in {gkey} -> {percent}% of {subtotal} = {d}")

    if cap > 0 and total_discount > cap:
        parts.append(f"cap {cap} applied (was {total_discount})")
        total_discount = cap

    return total_discount, "; ".join(parts)


def _apply_fixed_price_bundle(rule: Dict[str, Any], items: List[Dict[str, Any]]) -> Tuple[int, str]:
    """Apply FIXED_PRICE_BUNDLE rule.
    ruleParams supports two shapes:
      Single-tier (backward compatible):
        - bundleSize (int): number of items per bundle
        - fixedPricePence (int): target price in pence for each full bundle
        - repeatable (bool): apply for each full bundle or only once
        - capBundles (int): optional max bundles to apply (0 = unlimited)
      Multi-tier:
        - bundles: [ { bundleSize, fixedPricePence, capBundles? }, ... ]
        - repeatable (bool): apply greedily across tiers if true; else apply only the single best tier once

    Notes:
      - Items are grouped according to constraints (sameModeRequired, etc.).
      - Within each group, items are sorted by price descending.
      - Discount per bundle = max(0, sum(block) - fixedPricePence).
      - Multi-tier algorithm: greedy largest-first by bundleSize; honors per-tier capBundles and repeatable.
    Returns (discount_pence, explanation).
    """
    constraints = rule.get("constraints", {}) or {}
    params = rule.get("ruleParams", {}) or {}

    # Normalize into multi-tier structure if needed
    bundles_cfg = params.get("bundles")
    repeatable = bool(params.get("repeatable", True))
    if bundles_cfg and isinstance(bundles_cfg, list) and len(bundles_cfg) > 0:
        tiers = []
        for b in bundles_cfg:
            bs = _as_int((b or {}).get("bundleSize", 0), 0)
            fp = _as_int((b or {}).get("fixedPricePence", 0), 0)
            cap = _as_int((b or {}).get("capBundles", 0), 0)
            if bs > 0 and fp > 0:
                tiers.append({"bundleSize": bs, "fixedPricePence": fp, "capBundles": cap})
        # Sort tiers by bundleSize desc (greedy largest-first)
        tiers = sorted(tiers, key=lambda t: t["bundleSize"], reverse=True)
        if not tiers:
            return 0, "Invalid bundles configuration"
        smallest_bundle = min(t["bundleSize"] for t in tiers)
    else:
        # Single-tier fallback
        bs = _as_int(params.get("bundleSize", 0), 0)
        fp = _as_int(params.get("fixedPricePence", 0), 0)
        cap = _as_int(params.get("capBundles", 0), 0)
        if bs <= 0 or fp <= 0:
            return 0, "Invalid bundleSize/fixedPricePence"
        tiers = [{"bundleSize": max(1, bs), "fixedPricePence": fp, "capBundles": cap}]
        smallest_bundle = tiers[0]["bundleSize"]

    min_items_req = _as_int(constraints.get("minItems", 0), 0)

    # Group items per constraints
    groups: Dict[Tuple, List[Dict[str, Any]]] = {}
    for it in items:
        k = _group_key(it, constraints)
        groups.setdefault(k, []).append(it)

    total_discount = 0
    parts: List[str] = []

    for gkey, gitems in groups.items():
        count = len(gitems)
        need = max(min_items_req, smallest_bundle)
        if count < need:
            continue

        prices = sorted([_price_pence(it) for it in gitems if _price_pence(it) > 0], reverse=True)
        if not prices:
            continue

        group_disc = 0
        expl_bits: List[str] = []

        if repeatable:
            # Greedy largest-first across tiers
            # Track per-tier caps consumption
            caps_used = {i: 0 for i in range(len(tiers))}
            idx = 0
            # While we can fit any bundle from remaining items
            while True:
                progressed = False
                remaining = len(prices) - idx
                if remaining < smallest_bundle:
                    break
                for ti, t in enumerate(tiers):
                    bs = t["bundleSize"]
                    fp = t["fixedPricePence"]
                    cap = t.get("capBundles", 0)
                    if remaining < bs:
                        continue
                    if cap > 0 and caps_used[ti] >= cap:
                        continue
                    block = prices[idx: idx + bs]
                    if len(block) < bs:
                        continue
                    s = sum(block)
                    disc = max(0, s - fp)
                    group_disc += disc
                    caps_used[ti] += 1
                    expl_bits.append(f"bundle(size {bs}) {tuple(block)} -> (sum {s} - fixed {fp}) = {disc}")
                    idx += bs
                    progressed = True
                    break  # restart from largest tier again
                if not progressed:
                    break
        else:
            # Apply only the single best bundle once (choose tier with highest discount on top prices)
            best_disc = 0
            best_msg = None
            for t in tiers:
                bs = t["bundleSize"]
                fp = t["fixedPricePence"]
                if len(prices) < bs:
                    continue
                block = prices[:bs]
                s = sum(block)
                disc = max(0, s - fp)
                if disc > best_disc:
                    best_disc = disc
                    best_msg = f"bundle(size {bs}) {tuple(block)} -> (sum {s} - fixed {fp}) = {disc}"
            group_disc += best_disc
            if best_msg:
                expl_bits.append(best_msg)

        total_discount += group_disc
        parts.append(f"{count} items in {gkey} -> {len(expl_bits)} bundle(s): " + "; ".join(expl_bits))

    return total_discount, "; ".join(parts)

def _evaluate_rule(rule: Dict[str, Any], items: List[Dict[str, Any]]) -> RuleResult:
    # Filter items that match appliesTo
    matched = [it for it in items if _match_applies_to(rule, it)]
    discount = 0
    explanation = None
    rtype = rule.get("ruleType")
    rkind = (rtype or "").strip().upper()

    if rkind == "TIERED_PERCENT":
        discount, explanation = _apply_tiered_percent(rule, matched)
    elif rkind == "FIXED_PRICE_BUNDLE":
        discount, explanation = _apply_fixed_price_bundle(rule, matched)
    else:
        # Unknown rule: no discount
        discount = 0
        explanation = f"Unsupported ruleType '{rtype}'"

    return RuleResult(
        rule_id=str(rule.get("_id")),
        name=rule.get("name", ""),
        priority=int(rule.get("priority", 0)),
        ruleType=rtype or "",
        discount=int(discount),
        explanation=explanation,
    )


@router.post(
    "/basket/rate",
    response_model=RateBasketResponse,
    summary="Price a basket and apply the best discount rule",
    response_description="Subtotal, every rule considered, the winning rule, and the final total.",
    responses=secured({
        200: json_response(
            "The basket was priced. All amounts are in minor units.",
            {
                "basket_id": "68b2d1f0a4b21d0f8c9e8801",
                "subtotal": 14298,
                "eligible_rules": [
                    {
                        "rule_id": "68c0a1b2c3d4e5f6a7b8c9d0",
                        "name": "Multi-device 10% off",
                        "priority": 10,
                        "ruleType": "percentage",
                        "discount": 715,
                        "explanation": "2 qualifying devices — 10% off",
                    },
                    {
                        "rule_id": "68c0a1b2c3d4e5f6a7b8c9d1",
                        "name": "Three or more devices — £10 off",
                        "priority": 5,
                        "ruleType": "fixed",
                        "discount": 0,
                        "explanation": "Requires 3 devices, basket has 2",
                    },
                ],
                "best": {
                    "rule_id": "68c0a1b2c3d4e5f6a7b8c9d0",
                    "name": "Multi-device 10% off",
                    "priority": 10,
                    "ruleType": "percentage",
                    "discount": 715,
                    "explanation": "2 qualifying devices — 10% off",
                },
                "final_total": 13583,
            },
        ),
        400: error("`basket_id` is not a valid 24-character ObjectId.", "Invalid basket_id; must be a valid ObjectId string"),
        404: error("No basket with this id.", "Basket not found"),
    }),
)
def rate_basket(payload: RateBasketRequest, _: None = Depends(verify_token)):
    """
    Price a whole basket and apply the best available discount.

    Every active rule is evaluated against the basket's lines, and **exactly one wins** — the
    largest discount, with `priority` breaking ties. Discounts do **not** stack, which is why
    `eligible_rules` shows every rule considered (with `discount: 0` and an `explanation` for the
    ones that missed) while only `best` is deducted.

    All amounts are in **minor units** — pence or cents — so `14298` means £142.98. `final_total`
    is floored at `0`.

    Lines missing a `client` or `locale` inherit them from the basket root before rules are
    matched, so a line added without them still qualifies.

    `POST /basket/add` calls this automatically, so the totals on a basket are usually already
    current; call it directly after removing a line, since deletes do not re-rate.
    """
    # Fetch basket
    try:
        bid = ObjectId(payload.basket_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    basket = basket_collection.find_one({"_id": bid})
    if not basket:
        raise HTTPException(status_code=404, detail="Basket not found")

    items: List[Dict[str, Any]] = basket.get("Basket", []) or []
    # Fallback client/locale from basket root for rules matching if missing on items
    root_client = basket.get("client")
    root_locale = basket.get("locale")
    items_for_rules: List[Dict[str, Any]] = []
    for it in items:
        it2 = dict(it)
        if it2.get("client") is None and root_client is not None:
            it2["client"] = root_client
        if it2.get("locale") is None and root_locale is not None:
            it2["locale"] = root_locale
        items_for_rules.append(it2)
    subtotal_pence = sum(_price_pence(it) for it in items)

    # Load active rules
    rules = list(rules_collection.find({"active": True}))

    # Evaluate all rules
    results = [_evaluate_rule(r, items_for_rules) for r in rules]

    # Choose best rule by discount then priority (higher priority wins if same discount)
    best: Optional[RuleResult] = None
    for r in sorted(results, key=lambda rr: (-rr.discount, -rr.priority)):
        if r.discount > 0:
            best = r
            break

    discount = best.discount if best else 0
    final_total = max(0, subtotal_pence - discount)

    # Determine mode summary (single mode or 'mixed')
    modes = {it.get("mode") for it in items if it.get("mode") is not None}
    mode_value = next(iter(modes)) if len(modes) == 1 else "mixed"

    # Persist summary back to Basket_Quotes document
    try:
        basket_collection.update_one(
            {"_id": bid},
            {
                "$set": {
                    "subtotal": int(subtotal_pence),
                    "final_total": int(final_total),
                    "discount": int(discount),
                    "best_rule": best.dict() if best else None,
                    "mode": mode_value,
                }
            }
        )
    except Exception:
        # Non-blocking: still return computed response
        pass

    return RateBasketResponse(
        basket_id=str(basket["_id"]),
        subtotal=int(subtotal_pence),
        eligible_rules=results,
        best=best,
        final_total=int(final_total),
    )
