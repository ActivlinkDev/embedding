from fastapi import APIRouter, HTTPException, Query, Depends
from bson import ObjectId
from pymongo import MongoClient
import os
from dotenv import load_dotenv

from utils.dependencies import verify_token  # Token-based auth

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["SKU"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["MasterSKU"]

@router.get("/lookup_master_sku")
def lookup_master_sku(
    id: str = Query(..., description="The _id of the MasterSKU document"),
    locale: str = Query(..., description="Locale inside Locale_Specific_Data"),
    _: None = Depends(verify_token)
):
    try:
        object_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ObjectId format")

    query = {
        "_id": object_id,
        "Locale_Specific_Data.locale": locale
    }

  

    result = collection.find_one(query)

    if result:
        # Convert ObjectId to string
        result["_id"] = str(result["_id"])
        return result

    raise HTTPException(status_code=404, detail="No MasterSKU found for given ID and locale")
