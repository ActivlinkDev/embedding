from fastapi import APIRouter, HTTPException
import stripe
import os
from pymongo import MongoClient

from utils.api_docs import error, json_response

router = APIRouter(tags=["Payments"])

# Set your Stripe secret key
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# Set your MongoDB connection details
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["Activlink"]        # <- Use your actual database name
stripe_prices_col = db["Stripe_Price_ID"]

def serialize_price(price):
    return {
        "id": price["id"],
        "product": price["product"],
        "currency": price["currency"],
        "unit_amount": price.get("unit_amount"),
        "recurring": price.get("recurring"),
        "nickname": price.get("nickname"),
        "active": price.get("active"),
        "type": price.get("type"),
        "created": price.get("created"),
        "livemode": price.get("livemode"),
    }

@router.post(
    "/sync_stripe_prices",
    summary="Import new Stripe prices into the local catalogue",
    response_description="How many prices were newly imported, and which ones.",
    responses={
        200: json_response(
            "The sync completed. `inserted_count` is `0` when nothing new was found — the "
            "normal result of a repeat run.",
            {
                "inserted_count": 1,
                "inserted_prices": [
                    {
                        "id": "price_1QxYz123456",
                        "product": "prod_QxYz123456",
                        "currency": "gbp",
                        "unit_amount": 7149,
                        "recurring": None,
                        "nickname": "Extended warranty 24m",
                        "active": True,
                        "type": "one_time",
                        "created": 1786000000,
                        "livemode": False,
                    }
                ],
            },
        ),
        500: error("Stripe could not be reached, or the import failed part-way.", "Error syncing Stripe prices: ..."),
    },
)
def sync_stripe_prices():
    """
    **Operational endpoint.** Copy Stripe's active prices into the local `Stripe_Price_ID`
    collection.

    Takes no parameters. Every active price is paged through and any not already stored is
    inserted; existing records are **never updated or deleted**, so this only ever adds. Running
    it twice in a row is safe — the second run reports `inserted_count: 0`.

    Because it only inserts, a price changed or deactivated in Stripe will **not** be corrected
    here; the local copy keeps the values captured at first import.

    Only prices new to this run appear in `inserted_prices`. A failure part-way through leaves
    the prices already inserted in place — re-run to continue.
    """
    try:
        prices = []
        starting_after = None
        while True:
            response = stripe.Price.list(limit=100, starting_after=starting_after, active=True)
            for price in response["data"]:
                price_data = serialize_price(price)
                # Only insert if not already present in the collection
                if not stripe_prices_col.find_one({"id": price_data["id"]}):
                    stripe_prices_col.insert_one(price_data)
                    # Remove '_id' from the returned dict if present
                    price_data.pop("_id", None)
                    prices.append(price_data)
            if not response["has_more"]:
                break
            starting_after = response["data"][-1]["id"]
        return {"inserted_count": len(prices), "inserted_prices": prices}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error syncing Stripe prices: {str(e)}")
