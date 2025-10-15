from fastapi import APIRouter, Depends, HTTPException
from pymongo import MongoClient
from bson import ObjectId
import os

from utils.dependencies import verify_token

router = APIRouter(tags=["Customer"], prefix="")

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI not set in environment")

client = MongoClient(MONGO_URI)
db = client["Activlink"]
customer_collection = db["Customer"]


def _serialize_doc(doc: dict) -> dict:
    if not doc:
        return {}
    out = {}
    for k, v in doc.items():
        if k == "_id":
            try:
                out["_id"] = str(v)
            except Exception:
                out["_id"] = v
        else:
            out[k] = v
    return out


@router.get("/customer/lookup/by-id")
def get_customer_by_id(customer_id: str, _=Depends(verify_token)):
    """Simple authenticated endpoint to return a Customer document by id.

    Query: /customer/lookup/by-id?customer_id=<hexid>
    """
    try:
        objid = ObjectId(customer_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid customer_id")

    doc = customer_collection.find_one({"_id": objid})
    if not doc:
        raise HTTPException(status_code=404, detail="Customer not found")

    return {"data": _serialize_doc(doc)}
