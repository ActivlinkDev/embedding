from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import os
from utils.dependencies import caller_client_key, verify_token

router = APIRouter(tags=["Quotes"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
quotes_collection = db["Quotes"]


def _serialize_quote(doc):
    if not doc:
        return None
    out = dict(doc)
    _id = out.get("_id")
    if _id is not None:
        out["_id"] = str(_id)
    ca = out.get("created_at")
    if isinstance(ca, datetime):
        out["created_at"] = ca.isoformat()
    return out


@router.get("/quote/{quote_id}")
def get_quote(
    quote_id: str,
    _: None = Depends(verify_token),
    scope: str | None = Depends(caller_client_key),
):
    try:
        qid = ObjectId(quote_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid quote_id; must be a valid ObjectId string")

    # store_quote() writes the owner as camelCase clientKey, unlike contracts/orders.
    q = {"_id": qid}
    if scope:
        q["clientKey"] = scope

    doc = quotes_collection.find_one(q)
    if not doc:
        # 404 rather than 403: a pinned caller must not learn that another tenant's id exists.
        raise HTTPException(status_code=404, detail="Quote not found")
    return _serialize_quote(doc)
