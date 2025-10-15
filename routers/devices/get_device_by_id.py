from fastapi import APIRouter, Query, HTTPException
from pymongo import MongoClient
from bson import ObjectId
import os

router = APIRouter(tags=["Devices"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
devices_collection = db["Devices"]


def _serialize(doc: dict) -> dict:
    if not doc:
        return {}
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["_id"] = str(v)
        else:
            out[k] = v
    return out


@router.get("/devices/by-id")
def get_device_by_id(device_id: str = Query(..., alias="device_id")):
    try:
        objid = ObjectId(device_id)
    except Exception:
        # try as raw string id
        objid = None

    query = {"_id": objid} if objid is not None else {"_id": device_id}
    doc = devices_collection.find_one(query)
    if not doc:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"data": _serialize(doc)}
