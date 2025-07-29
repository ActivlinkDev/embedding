from fastapi import APIRouter, Body
from pymongo import MongoClient
import os

router = APIRouter(tags=["Customer"])

# Setup Mongo client and collection
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
customer_collection = db["Customer"]

# --- Reusable Function ---
def get_or_create_customer(
    collection,
    name: str,
    telephone: str,
    email: str
) -> (str, bool):
    """
    Checks if a customer exists by telephone or email (case-insensitive).
    Returns (customer_id, existing: bool).
    If not found, creates and returns new id.
    """
    query = {
        "$or": [
            {"telephone": telephone},
            {"email": {"$regex": f"^{email}$", "$options": "i"}}
        ]
    }
    existing = collection.find_one(query)
    if existing:
        return str(existing["_id"]), True
    customer_doc = {"name": name, "telephone": telephone, "email": email}
    result = collection.insert_one(customer_doc)
    return str(result.inserted_id), False

# --- FastAPI Endpoint using the function ---
@router.post("/get-or-create-customer")
def get_or_create_customer_endpoint(
    name: str = Body(...),
    telephone: str = Body(...),
    email: str = Body(...)
):
    customer_id, existing = get_or_create_customer(
        customer_collection, name, telephone, email
    )
    return {"customerId": customer_id, "existing": existing}

# --- You can use the function elsewhere in the file too ---
def use_customer():
    cid, exists = get_or_create_customer(
        customer_collection, "Bob", "5550000", "bob@example.com"
    )
    print("CustomerID:", cid, "| Exists:", exists)
