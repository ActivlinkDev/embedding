from fastapi import APIRouter, Query, HTTPException, Depends
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
    # Recursively serialize a document, converting ObjectId to str anywhere in the structure.
    from bson import ObjectId as _ObjectId

    def _serialize_value(value):
        if isinstance(value, _ObjectId):
            return str(value)
        if isinstance(value, dict):
            return {k: _serialize_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_serialize_value(v) for v in value]
        # leave other types (datetime, numbers, strings) as-is
        return value

    if not doc:
        return {}
    return _serialize_value(doc)


@router.get("/customer/by-id")
def get_customer_by_id(customer_id: str = Query(..., alias="customer_id"), _=Depends(verify_token)):
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

        # Exclude transaction_log from API responses
        if isinstance(doc, dict) and "transaction_log" in doc:
            try:
                del doc["transaction_log"]
            except Exception:
                # ignore if deletion fails for any reason
                pass

        return {"data": _serialize_doc(doc)}
    except HTTPException:
        raise
    except Exception as e:
        # Development helper: return exception details for debugging
        raise HTTPException(status_code=500, detail=f"Internal error: {type(e).__name__}: {e}")
