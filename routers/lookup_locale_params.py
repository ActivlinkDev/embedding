from fastapi import APIRouter, HTTPException, Query
from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(
    prefix="/locale",
    tags=["Lookup Locale Details"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["Locale_Params"]  # ✅ your target collection

@router.get("/locale-details", tags=["Lookup Locale Details"])
def get_locale_details(locale: str = Query(..., description="Locale code to look up (e.g. en_GB)")):
    result = collection.find_one({"locale": locale}, {"_id": 0})
    
    if result:
        return result
    else:
        raise HTTPException(status_code=404, detail="No details found for the specified locale")
