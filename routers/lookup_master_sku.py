from fastapi import APIRouter, HTTPException, Query, Depends
from bson import ObjectId
from pymongo import MongoClient
import os
from dotenv import load_dotenv

from utils.dependencies import verify_token  # ✅ Token-based auth like your other routes

load_dotenv()

router = APIRouter(
    prefix="/master-sku",
    tags=["Lookup Master SKU"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["MasterSKU"]

@router.get("/lookup")
def lookup_master_sku(
    id: str = Query(..., description="The _id of the MasterSKU document"),
    locale: str = Query(..., description="Locale inside Locale_Specific_Data"),
    _: None = Depends(verify_token)  # ✅ Require token auth
):
    try:
        object_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ObjectId format")

    query = {
        "_id": object_id,
        "Locale_Specific_Data.locale": locale
    }

    projection = {
        "Locale_Specific_Data.$": 1
    }

    result = collection.find_one(query, projection)

    if result:
        return result
    else:
        raise HTTPException(status_code=404, detail="No MasterSKU found for given ID and locale")
