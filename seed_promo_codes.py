"""Seed example promo codes into the Mongo `PromoCodes` collection.

Usage:
    MONGO_URI="mongodb://..." python seed_promo_codes.py

Codes live in MongoDB (mirroring `BundleDiscountRules`), not Strapi. This script
upserts a couple of example codes so the basket promo flow can be tested
end-to-end. Edit / extend the EXAMPLES list as needed, or use the token-guarded
`POST /promo/upsert` API endpoint for production management.

Document shape (all money in pence):
    code            unique, stored upper-cased
    active          bool
    discountType    "PERCENT" (value = whole percent) | "FIXED" (value = pence off)
    value           int
    appliesTo       { currency, locale, client, productIds, categoryGroups, mode }
    constraints     { minSubtotalPence, minItems }
    capAmountPence  optional cap for PERCENT (0 = none)
    maxRedemptions  0 = unlimited
    redemptions     running counter (set only on insert)
    validFrom/To    ISO string or null
    priority        int
"""

import os
from pymongo import MongoClient

EXAMPLES = [
    {
        "code": "WELCOME10",
        "active": True,
        "discountType": "PERCENT",
        "value": 10,
        "appliesTo": {"currency": [], "locale": [], "client": [],
                      "productIds": [], "categoryGroups": [], "mode": "any"},
        "constraints": {"minSubtotalPence": 0, "minItems": 0},
        "capAmountPence": 0,
        "maxRedemptions": 0,
        "validFrom": None,
        "validTo": None,
        "priority": 0,
    },
    {
        "code": "SAVE5",
        "active": True,
        "discountType": "FIXED",
        "value": 500,  # £5.00 off
        "appliesTo": {"currency": ["GBP"], "locale": [], "client": [],
                      "productIds": [], "categoryGroups": [], "mode": "any"},
        "constraints": {"minSubtotalPence": 2000, "minItems": 1},  # min £20 basket
        "capAmountPence": 0,
        "maxRedemptions": 100,
        "validFrom": None,
        "validTo": None,
        "priority": 0,
    },
]


def main():
    uri = os.getenv("MONGO_URI")
    if not uri:
        raise SystemExit("MONGO_URI must be set")
    db = MongoClient(uri)["Activlink"]
    coll = db["PromoCodes"]
    for ex in EXAMPLES:
        ex["code"] = ex["code"].strip().upper()
        coll.update_one(
            {"code": ex["code"]},
            {"$set": ex, "$setOnInsert": {"redemptions": 0}},
            upsert=True,
        )
        print(f"Upserted promo code {ex['code']}")
    print("Done.")


if __name__ == "__main__":
    main()
