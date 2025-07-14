from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import os
from dotenv import load_dotenv

from utils.dependencies import verify_token  # ✅ Token-based auth

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Lookup Master SKU All"],
    dependencies=[Depends(verify_token)]  # ✅ Apply token auth to all routes in this router
)

# MongoDB connection
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["MasterSKU"]

@router.get("/lookup_master_sku_all")
def lookup_master_sku(
    id: Optional[str] = Query(None, description="MongoDB ObjectId"),
    GTIN: Optional[str] = Query(None, description="GTIN code"),
    Make: Optional[str] = Query(None, description="Product manufacturer"),
    Model: Optional[str] = Query(None, description="Product model number")
):
    # 1. Match by MongoDB ObjectId
    if id:
        try:
            object_id = ObjectId(id)
            result = collection.find_one({"_id": object_id})
            if result:
                result["_id"] = str(result["_id"])
                return {"matched_by": "_id", "result": result}
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ObjectId format")

    # 2. Match by GTIN (supports GTIN as array or single value)
    if GTIN:
        result = collection.find_one({"GTIN": {"$in": [GTIN]}})
        if result:
            result["_id"] = str(result["_id"])
            return {"matched_by": "GTIN", "result": result}

    # 3. Match by Make & Model (case-insensitive)
    if Make and Model:
        result = collection.find_one({
            "Make": {"$regex": f"^{Make}$", "$options": "i"},
            "Model": {"$regex": Model, "$options": "i"}
        })
        if result:
            result["_id"] = str(result["_id"])
            return {"matched_by": "Make+Model", "result": result}

    raise HTTPException(status_code=404, detail="No matching MasterSKU found")
