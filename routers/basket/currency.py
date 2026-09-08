"""Currency validation shared by basket writes and checkout."""
from fastapi import HTTPException

MIXED_CURRENCY_DETAIL = "Items in your basket must use the same currency. Remove items in other currencies to continue, then check them out separately."

def normalize_currency(value):
    return str(value or "").strip().upper() or "GBP"

def basket_currency(items):
    currencies = {normalize_currency(item.get("currency")) for item in items}
    if len(currencies) > 1:
        raise HTTPException(status_code=409, detail=MIXED_CURRENCY_DETAIL)
    return next(iter(currencies), "GBP")

def currency_guard_filter(currency):
    # Atomic comparison prevents simultaneous additions from mixing currencies.
    return {"$expr": {"$allElementsTrue": [{"$map": {
        "input": {"$ifNull": ["$Basket", []]}, "as": "item",
        "in": {"$eq": [{"$let": {
            "vars": {"code": {"$toUpper": {"$trim": {"input": {"$ifNull": ["$$item.currency", ""]}}}}},
            "in": {"$cond": [{"$eq": ["$$code", ""]}, "GBP", "$$code"]}
        }}, normalize_currency(currency)]}
    }}]}}
