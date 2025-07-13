# routers/client_lookup.py

from fastapi import APIRouter, HTTPException, Query
from pymongo import MongoClient
import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

router = APIRouter()

# MongoDB connection
MONGO_URI = os.getenv("MONGO_URI", "your-default-mongo-uri")
client = MongoClient(MONGO_URI)
db = client["Activlink"]
collection = db["ClientKey"]

@router.get("/get-client", tags=["Client Lookup"])
def get_client(clientkey: str = Query(..., description="The clientkey to look up the full client record")):
    result = collection.find_one({"ClientKey": clientkey}, {"_id": 0})

    if result:
        return result
    else:
        raise HTTPException(status_code=404, detail="Client not found for the given clientkey")
