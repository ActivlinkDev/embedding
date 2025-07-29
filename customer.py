from pymongo import MongoClient
from bson import ObjectId

def get_or_create_customer(name: str, telephone: str, email: str):
    # Connect to your MongoDB as per your env/settings
    client = MongoClient(os.getenv("MONGO_URI"))
    db = client["Activlink"]
    customer_collection = db["Customer"]

    # 1. Check for existing by telephone or email (case-insensitive for email)
    query = {
        "$or": [
            {"telephone": telephone},
            {"email": {"$regex": f"^{email}$", "$options": "i"}}
        ]
    }
    existing = customer_collection.find_one(query)

    if existing:
        return str(existing["_id"])

    # 2. Create if not exists
    customer_doc = {
        "name": name,
        "telephone": telephone,
        "email": email
    }
    result = customer_collection.insert_one(customer_doc)
    return str(result.inserted_id)
