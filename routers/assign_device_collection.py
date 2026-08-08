from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional
from bson import ObjectId
from pymongo import MongoClient
import os

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

router = APIRouter(tags=["Assignments"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
device_collection = db["Device_Collection"]
devices_collection = db["Devices"]

class AssignDeviceToCollectionRequest(BaseModel):
    """A set of already-registered devices to group under one shareable collection."""

    client: str = Field(
        ...,
        description="**Mandatory.** `Client_ID` the collection belongs to.",
        examples=["ACME-UK"],
    )
    devices: List[str] = Field(
        ...,
        description=(
            "**Mandatory.** Device ids (24-character ObjectIds) returned by "
            "`POST /device-register`. Every id must exist and none may already belong to "
            "another collection."
        ),
        examples=[["68881375d4d368937a0f887d", "68881375d4d368937a0f887e"]],
    )
    customerID: Optional[str] = Field(
        None,
        description="Optional customer to attach the collection to. Omitted from the stored document when null.",
        examples=["6820f1c9a4b21d0f8c9e9001"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "client": "ACME-UK",
                "devices": ["68881375d4d368937a0f887d", "68881375d4d368937a0f887e"],
                "customerID": "6820f1c9a4b21d0f8c9e9001",
            }
        }
    }


@router.post(
    "/assign_device_to_collection",
    summary="Group registered devices into a shareable collection",
    response_description="The stored collection document, including its id and landing URL.",
    responses=secured({
        200: json_response(
            "The collection was created.",
            {
                "client": "ACME-UK",
                "devices": ["68881375d4d368937a0f887d", "68881375d4d368937a0f887e"],
                "status": "created",
                "customerID": "6820f1c9a4b21d0f8c9e9001",
                "_id": "6889a1c2d4d368937a0f8912",
                "URL": "http://www.activlink.io/?id=6889a1c2d4d368937a0f8912",
            },
        ),
        400: error("One or more entries in `devices` is not a valid device id.", "One or more device IDs are invalid."),
        404: error(
            "One or more device ids do not exist. The message names them.",
            "The following device IDs do not exist in Devices collection: 68881375d4d368937a0f887e",
        ),
        409: error(
            "One or more devices already belong to a collection. The message names them.",
            "The following device IDs already exist in a collection: 68881375d4d368937a0f887d",
        ),
    }),
)
def assign_device_to_collection(
    req: AssignDeviceToCollectionRequest, _: None = Depends(verify_token)
):
    """
    Group one or more registered devices into a single `Device_Collection`, and get back a
    shareable URL the customer can use to complete cover for all of them at once.

    **All-or-nothing.** The whole request is rejected if any device id is malformed (`400`),
    unknown (`404`), or already assigned to another collection (`409`) — nothing is written in
    those cases. A device therefore belongs to at most one collection.

    On success the collection is stored with `status: "created"` and a generated `URL`, both of
    which are returned along with the new `_id`.
    """
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
