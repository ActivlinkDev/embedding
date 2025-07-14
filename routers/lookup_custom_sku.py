from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import os
from dotenv import load_dotenv

from utils.dependencies import verify_token  # ✅ Match your existing auth pattern

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["SKU Lookup"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["CustomSKU"]

@router.get("/lookup")
def lookup_sku(
    locale: str = Query(..., description="Locale inside Locale_Specific_Data"),
    client: str = Query(..., description="Client name to match (required)"),
    Make: Optional[str] = None,
    Model: Optional[str] = None,
    GTIN: Optional[str] = None,
    SKU: Optional[str] = None,
    id: Optional[str] = None,
    _: None = Depends(verify_token)  # ✅ Enforces token authentication
):
    base_query = {
        "Locale_Specific_Data": {
            "$elemMatch": {"locale": locale}
        },
        "Client": client
    }

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

    if GTIN:
        result = find_with({"Identifiers.GTIN": GTIN}, "GTIN")
        if result:
            return result

    if SKU:
        result = find_with({"Identifiers.SKU": SKU}, "SKU")
        if result:
            return result

    if Make and Model:
        result = find_with({
            "Identifiers.Make": {"$regex": f"^{Make}$", "$options": "i"},
            "Identifiers.Model": {"$regex": Model, "$options": "i"}
        }, "Make+Model (fuzzy)")
        if result:
            return result

    raise HTTPException(status_code=404, detail="No matching SKU found using provided parameters.")
