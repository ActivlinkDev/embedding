from fastapi import APIRouter, Query, HTTPException
from pymongo import MongoClient
from bson import ObjectId
import os

router = APIRouter(tags=["Customer"])

client = MongoClient(os.getenv("MONGO_URI"))
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


@router.get("/customer/by-id")
def get_customer_by_id(customer_id: str = Query(..., alias="customer_id")):
    """Return a customer document by its id (string).

    Query param: ?customer_id=<hexid>
    """
    try:
        try:
            objid = ObjectId(customer_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid customer_id")

        doc = customer_collection.find_one({"_id": objid})
        if not doc:
            raise HTTPException(status_code=404, detail="Customer not found")

        return {"data": _serialize_doc(doc)}
    except HTTPException:
        # re-raise expected HTTPExceptions
        raise
    except Exception as e:
        # Development-only: return exception details to help debugging (replace with logging in prod)
        raise HTTPException(status_code=500, detail=f"Internal error: {type(e).__name__}: {str(e)}")
