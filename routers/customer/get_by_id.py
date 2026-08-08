from fastapi import APIRouter, Query, HTTPException, Depends
from pymongo import MongoClient
from bson import ObjectId
import os

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

router = APIRouter(tags=["Customers"], prefix="")

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


@router.get(
    "/customer/by-id",
    summary="Fetch a customer record",
    response_description="The customer document, wrapped in `data`, without its transaction log.",
    responses=secured({
        200: json_response(
            "The customer was found.",
            {
                "data": {
                    "_id": "6820f1c9a4b21d0f8c9e9001",
                    "name": "Jane Okafor",
                    "telephone": "+447700900123",
                    "email": "jane.okafor@example.com",
                    "devices": [
                        {"deviceId": "6820f1c9a4b21d0f8c9e4471", "status": "contract"},
                        {"deviceId": "6820f1c9a4b21d0f8c9e4472", "status": "registered"},
                    ],
                }
            },
        ),
        400: error("`customer_id` is not a valid 24-character ObjectId.", "Invalid customer_id"),
        404: error("No customer with this id.", "Customer not found"),
        500: error("The lookup failed unexpectedly.", "Internal error: ..."),
    }),
)
def get_customer_by_id(
    customer_id: str = Query(
        ...,
        alias="customer_id",
        description="**Mandatory.** The customer's `_id` (24-character ObjectId).",
        examples=["6820f1c9a4b21d0f8c9e9001"],
    ),
    _=Depends(verify_token),
):
    """
    Fetch a customer record by id.

    The document is returned as stored, with every nested `ObjectId` converted to a string —
    **except `transaction_log`, which is always stripped** from the response. Do not rely on this
    endpoint for payment history; use `GET /customers/{customer_id}/orders`.

    `devices` carries the pairing produced by `POST /pair-customer`: `contract` for devices with
    cover, `registered` for devices where it was declined.

    `customer_id` is mandatory.
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
