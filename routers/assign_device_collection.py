from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional
from bson import ObjectId
from pymongo import MongoClient
import os

from utils.dependencies import verify_token

router = APIRouter(tags=["Device Collection"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
device_collection = db["Device_Collection"]
devices_collection = db["Devices"]

class AssignDeviceToCollectionRequest(BaseModel):
    client: str
    devices: List[str] = Field(..., example=["68881375d4d368937a0f887d"])
    customerID: Optional[str] = None

@router.post("/assign_device_to_collection")
def assign_device_to_collection(
    req: AssignDeviceToCollectionRequest, _: None = Depends(verify_token)
):
    # 1. Validate all device ObjectIds
    try:
        object_ids = [ObjectId(device_id) for device_id in req.devices]
    except Exception:
        raise HTTPException(status_code=400, detail="One or more device IDs are invalid.")

    # 2. Check that all device IDs exist in Devices collection
    found_devices = set(
        str(doc["_id"]) for doc in devices_collection.find({"_id": {"$in": object_ids}})
    )
    missing_ids = [dev_id for dev_id in req.devices if dev_id not in found_devices]
    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail=f"The following device IDs do not exist in Devices collection: {', '.join(missing_ids)}"
        )

    # 3. Check for duplicate device IDs in Device_Collection
    duplicate_ids = []
    for device_id in req.devices:
        exists = device_collection.find_one({"devices": device_id})
        if exists:
            duplicate_ids.append(device_id)
    if duplicate_ids:
        raise HTTPException(
            status_code=409,
            detail=f"The following device IDs already exist in a collection: {', '.join(duplicate_ids)}"
        )

    # 4. Create the collection document
    collection_doc = {
        "client": req.client,
        "devices": req.devices,
        "status": "created"
    }
    if req.customerID:
        collection_doc["customerID"] = req.customerID

    result = device_collection.insert_one(collection_doc)
    url = f"http://www.activlink.io/?id={str(result.inserted_id)}"
    device_collection.update_one({"_id": result.inserted_id}, {"$set": {"URL": url}})
    collection_doc["_id"] = str(result.inserted_id)
    collection_doc["URL"] = url

    return collection_doc
