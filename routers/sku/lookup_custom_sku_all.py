from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import os
from dotenv import load_dotenv

from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["SKU"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["CustomSKU"]

@router.get("/lookup_custom_all")
def lookup_sku(
    id: Optional[str] = None,
    client: Optional[str] = Query(None, description="Client name required for GTIN, SKU, Make+Model searches"),
    Make: Optional[str] = None,
    Model: Optional[str] = None,
    GTIN: Optional[str] = None,
    SKU: Optional[str] = None,
    _: None = Depends(verify_token)
):
    # 1. Try match by _id without client
    if id:
        try:
            object_id = ObjectId(id)
            result = collection.find_one({"_id": object_id}, {"_id": 0})
            if result:
                return {
                    "matched_by": "_id",
                    "count": 1,
                    "results": [result]
                }
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ObjectId format")

    # If any other search is attempted, client becomes required
    if not client:
        raise HTTPException(status_code=400, detail="`client` is required when not searching by _id.")

    base_query = {
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
