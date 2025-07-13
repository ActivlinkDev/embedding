from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import os
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

# MongoDB connection
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["CustomSKU"]  # ✅ Updated collection name

@router.get("/lookup-sku", tags=["SKU Lookup"])
def lookup_sku(
    locale: str = Query(..., description="Locale inside Locale_Specific_Data"),
    Make: Optional[str] = None,
    Model: Optional[str] = None,
    GTIN: Optional[str] = None,
    SKU: Optional[str] = None,
    id: Optional[str] = None,
    client: Optional[str] = None
):
    # Always match documents with this locale inside Locale_Specific_Data
    base_query = {
        "Locale_Specific_Data": {
            "$elemMatch": {"locale": locale}
        }
    }
    if client:
        base_query["Client"] = client

    def find_with(extra_query, matched_by):
        full_query = {**base_query, **extra_query}
        results = list(collection.find(full_query, {"_id": 0}))
        if results:
            return {
                "matched_by": matched_by,
                "count": len(results),
                "results": results
            }
        return None

    # 1. Match by MongoDB _id
    if id:
        try:
            object_id = ObjectId(id)
            full_query = {**base_query, "_id": object_id}
            results = list(collection.find(full_query, {"_id": 0}))
            if results:
                return {
                    "matched_by": "_id",
                    "count": len(results),
                    "results": results
                }
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ObjectId format")

    # 2. Match by GTIN (as exact match in string or array)
    if GTIN:
        result = find_with({"Identifiers.GTIN": GTIN}, "GTIN")
        if result:
            return result

    # 3. Match by SKU
    if SKU:
        result = find_with({"Identifiers.SKU": SKU}, "SKU")
        if result:
            return result

    # 4. Match by Make (exact, case-insensitive) + Model (fuzzy, case-insensitive)
    if Make and Model:
        result = find_with({
            "Identifiers.Make": {"$regex": f"^{Make}$", "$options": "i"},
            "Identifiers.Model": {"$regex": Model, "$options": "i"}
        }, "Make+Model (fuzzy)")
        if result:
            return result

    raise HTTPException(status_code=404, detail="No matching SKU found using provided parameters.")
