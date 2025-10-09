from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional, Any, Dict
from pymongo import MongoClient
from bson import ObjectId
import os
from utils.dependencies import verify_token

router = APIRouter(tags=["Device"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
devices_collection = db["Devices"]


class DeviceIdRequest(BaseModel):
    device_id: str = Field(..., description="Devices._id as string (ObjectId)")


def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return doc
    out = dict(doc)
    _id = out.get("_id")
    if _id is not None:
        out["_id"] = str(_id)
    return out


@router.get("/device/{device_id}")
def get_device(device_id: str, _: None = Depends(verify_token)):
    try:
        oid = ObjectId(device_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid device_id; must be a valid ObjectId string")

    doc = devices_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Device not found")
    return _serialize(doc)


@router.post("/device/by-id")
def post_get_device(payload: DeviceIdRequest, _: None = Depends(verify_token)):
    try:
        oid = ObjectId(payload.device_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid device_id; must be a valid ObjectId string")

    doc = devices_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Device not found")
    return _serialize(doc)
