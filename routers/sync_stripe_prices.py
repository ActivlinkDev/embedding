from fastapi import APIRouter, HTTPException
import stripe
import os
from pymongo import MongoClient

router = APIRouter(tags=["Stripe Utilities"])

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

@router.post("/sync_stripe_prices")
def sync_stripe_prices():
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
