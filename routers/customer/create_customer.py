from fastapi import APIRouter, Body
from pymongo import MongoClient
import os

from utils.api_docs import json_response

router = APIRouter(tags=["Customers"])

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
@router.post(
    "/get-or-create-customer",
    summary="Find a customer by phone or email, creating one if needed",
    response_description="The customer id, and whether they already existed.",
    responses={
        200: json_response(
            "A customer id, either matched or newly created. `existing` tells you which.",
            {"customerId": "6820f1c9a4b21d0f8c9e9001", "existing": False},
        ),
    },
)
def get_or_create_customer_endpoint(
    name: str = Body(..., description="**Mandatory.** Customer's full name. Only used when creating a new record.", examples=["Jane Okafor"]),
    telephone: str = Body(..., description="**Mandatory.** Phone number, matched **exactly** as given.", examples=["+447700900123"]),
    email: str = Body(..., description="**Mandatory.** Email address, matched case-insensitively.", examples=["jane.okafor@example.com"])
):
    """
    Look up a customer by phone **or** email, and create one if neither matches.

    All three fields are mandatory. Matching is an **OR**: a record whose `telephone` matches
    exactly, or whose `email` matches case-insensitively, is returned as-is. Note that phone
    matching is a literal string comparison — `+447700900123` and `07700 900123` are treated as
    different people, so normalise to E.164 before calling.

    `existing: true` means an existing record was returned and **nothing was updated** — a
    changed name or email on a matched customer is silently ignored. `existing: false` means a
    new customer was created from all three fields.

    **This endpoint is not authenticated.** It is called during checkout, including from the
    Stripe webhook. Only the id is returned, never the customer record.
    """
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
