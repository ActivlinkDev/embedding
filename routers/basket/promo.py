from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId
from datetime import datetime
import os

from utils.dependencies import verify_token
from .ratebasket import (
    rate_basket,
    RateBasketRequest,
    _match_applies_to,
    _price_pence,
    _as_int,
)
from . import _serialize_basket_doc

router = APIRouter(tags=["Basket"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
basket_collection = db["Basket_Quotes"]
promo_collection = db["PromoCodes"]


# ---- helpers ----

def _coerce_dt(val: Any) -> Optional[datetime]:
    """Accept datetime or ISO-8601 string; return naive UTC datetime or None."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            # Tolerate trailing 'Z'
            return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None
    return None


def validate_and_compute_promo(
    code: Optional[str],
    items: List[Dict[str, Any]],
    root_client: Optional[str] = None,
    root_locale: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate a promo code against the given basket items and compute a discount.

    Pure helper (no DB writes). Returns a dict:
      { valid, code, discountType, value, discount_pence, message }
    `message` carries a human-readable reason when invalid.
    """
    norm = (code or "").strip().upper()
    result: Dict[str, Any] = {
        "valid": False,
        "code": norm,
        "discountType": None,
        "value": 0,
        "discount_pence": 0,
        "message": "",
    }

    if not norm:
        result["message"] = "No code provided"
        return result

    promo = promo_collection.find_one({"code": norm})
    if not promo:
        result["message"] = "Invalid code"
        return result
    if not promo.get("active", False):
        result["message"] = "Code is not active"
        return result

    now = datetime.utcnow()
    vf_dt = _coerce_dt(promo.get("validFrom"))
    vt_dt = _coerce_dt(promo.get("validTo"))
    if vf_dt and now < vf_dt:
        result["message"] = "Code not yet valid"
        return result
    if vt_dt and now > vt_dt:
        result["message"] = "Code expired"
        return result

    max_red = _as_int(promo.get("maxRedemptions", 0), 0)
    red = _as_int(promo.get("redemptions", 0), 0)
    if max_red > 0 and red >= max_red:
        result["message"] = "Code redemption limit reached"
        return result

    # Enrich items with root client/locale so appliesTo matching is consistent
    # with the bundle-rule engine (see ratebasket.rate_basket).
    enriched: List[Dict[str, Any]] = []
    for it in items:
        it2 = dict(it)
        if it2.get("client") is None and root_client is not None:
            it2["client"] = root_client
        if it2.get("locale") is None and root_locale is not None:
            it2["locale"] = root_locale
        enriched.append(it2)

    matched = [it for it in enriched if _match_applies_to(promo, it)]
    if not matched:
        result["message"] = "Code does not apply to the items in your basket"
        return result

    eligible_subtotal = sum(_price_pence(it) for it in matched)

    constraints = promo.get("constraints", {}) or {}
    min_items = _as_int(constraints.get("minItems", 0), 0)
    min_sub = _as_int(constraints.get("minSubtotalPence", 0), 0)
    if min_items > 0 and len(matched) < min_items:
        result["message"] = f"Minimum {min_items} items required for this code"
        return result
    if min_sub > 0 and eligible_subtotal < min_sub:
        result["message"] = "Basket total too low for this code"
        return result

    dtype = (promo.get("discountType") or "").strip().upper()
    value = _as_int(promo.get("value", 0), 0)
    discount = 0
    if dtype == "PERCENT":
        discount = int(eligible_subtotal * value / 100)
        cap = _as_int(promo.get("capAmountPence", 0), 0)
        if cap > 0 and discount > cap:
            discount = cap
    elif dtype == "FIXED":
        discount = min(value, eligible_subtotal)
    else:
        result["message"] = f"Unsupported discountType '{dtype}'"
        return result

    if discount <= 0:
        result["message"] = "No discount applicable for this code"
        return result

    result.update({
        "valid": True,
        "discountType": dtype,
        "value": value,
        "discount_pence": int(discount),
        "message": "Applied",
    })
    return result


# ---- request models ----

class ApplyPromoRequest(BaseModel):
    basket_id: str = Field(..., description="Basket_Quotes _id as string")
    code: str = Field(..., description="Promo code to apply")


class RemovePromoRequest(BaseModel):
    basket_id: str = Field(..., description="Basket_Quotes _id as string")


# ---- customer-facing endpoints ----

@router.post("/basket/promo/apply")
def apply_promo(req: ApplyPromoRequest, _: None = Depends(verify_token)):
    try:
        bid = ObjectId(req.basket_id.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    basket = basket_collection.find_one({"_id": bid})
    if not basket:
        raise HTTPException(status_code=404, detail="Basket not found")

    items = basket.get("Basket", []) or []
    if not items:
        raise HTTPException(status_code=400, detail="Basket is empty")

    promo = validate_and_compute_promo(
        req.code,
        items,
        root_client=basket.get("client"),
        root_locale=basket.get("locale"),
    )
    if not promo.get("valid"):
        raise HTTPException(status_code=400, detail=promo.get("message") or "Invalid code")

    # Persist the applied promo, then re-rate (rate_basket folds it in best-of).
    basket_collection.update_one({"_id": bid}, {"$set": {"applied_promo": promo}})
    try:
        rate_basket(RateBasketRequest(basket_id=str(bid)))
    except Exception:
        pass

    doc = basket_collection.find_one({"_id": bid})
    return _serialize_basket_doc(doc)


@router.post("/basket/promo/remove")
def remove_promo(req: RemovePromoRequest, _: None = Depends(verify_token)):
    try:
        bid = ObjectId(req.basket_id.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id; must be a valid ObjectId string")

    result = basket_collection.find_one_and_update(
        {"_id": bid},
        {"$unset": {"applied_promo": ""}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Basket not found")

    try:
        rate_basket(RateBasketRequest(basket_id=str(bid)))
    except Exception:
        pass

    doc = basket_collection.find_one({"_id": bid})
    return _serialize_basket_doc(doc)


# ---- admin endpoints (token-guarded) ----

class UpsertPromoRequest(BaseModel):
    code: str = Field(..., description="Promo code (stored upper-cased)")
    active: bool = True
    discountType: str = Field(..., description='"PERCENT" or "FIXED"')
    value: int = Field(..., description="PERCENT: whole percent (10 = 10%). FIXED: pence off.")
    appliesTo: Optional[Dict[str, Any]] = None
    constraints: Optional[Dict[str, Any]] = None
    capAmountPence: int = 0
    maxRedemptions: int = 0
    validFrom: Optional[str] = None
    validTo: Optional[str] = None
    priority: int = 0


@router.post("/promo/upsert")
def upsert_promo(req: UpsertPromoRequest, _: None = Depends(verify_token)):
    norm = req.code.strip().upper()
    if not norm:
        raise HTTPException(status_code=400, detail="code is required")
    dtype = req.discountType.strip().upper()
    if dtype not in ("PERCENT", "FIXED"):
        raise HTTPException(status_code=400, detail="discountType must be PERCENT or FIXED")

    fields: Dict[str, Any] = {
        "code": norm,
        "active": bool(req.active),
        "discountType": dtype,
        "value": int(req.value),
        "appliesTo": req.appliesTo or {
            "currency": [], "locale": [], "client": [],
            "productIds": [], "categoryGroups": [], "mode": "any",
        },
        "constraints": req.constraints or {"minSubtotalPence": 0, "minItems": 0},
        "capAmountPence": int(req.capAmountPence),
        "maxRedemptions": int(req.maxRedemptions),
        "validFrom": req.validFrom,
        "validTo": req.validTo,
        "priority": int(req.priority),
    }

    promo_collection.update_one(
        {"code": norm},
        {
            "$set": fields,
            "$setOnInsert": {"redemptions": 0},
        },
        upsert=True,
    )
    doc = promo_collection.find_one({"code": norm})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


@router.get("/promo/list")
def list_promos(_: None = Depends(verify_token)):
    out = []
    for doc in promo_collection.find({}):
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return {"count": len(out), "items": out}


class DeactivatePromoRequest(BaseModel):
    code: str = Field(..., description="Promo code to deactivate")


@router.post("/promo/deactivate")
def deactivate_promo(req: DeactivatePromoRequest, _: None = Depends(verify_token)):
    norm = req.code.strip().upper()
    result = promo_collection.update_one({"code": norm}, {"$set": {"active": False}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Promo code not found")
    return {"code": norm, "active": False}
