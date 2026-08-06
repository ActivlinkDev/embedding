from fastapi import APIRouter, Query, HTTPException
from pymongo import MongoClient
from bson import ObjectId
import os

from utils.api_docs import error, json_response

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


@router.get(
    "/devices/by-id",
    summary="Fetch a registered device by its id",
    response_description="The stored device document, wrapped in `data`.",
    responses={
        200: json_response(
            "The device was found.",
            {
                "data": {
                    "_id": "6820f1c9a4b21d0f8c9e4471",
                    "client": "ACME-UK",
                    "locale": "en_GB",
                    "source": "web",
                    "identifiers": {
                        "GTIN": "5011773057240",
                        "make": "Bosch",
                        "model": "SMS6ZCI00G",
                        "title": "Bosch Series 6 Freestanding Dishwasher",
                        "category": "Dishwasher",
                        "gteeParts": "24",
                        "gteeLabour": "12",
                    },
                    "uniqueParameters": {"MAC": "", "serial": "SN-8841203", "imei": ""},
                    "registrationParameters": {
                        "purchaseDate": "2025-05-01",
                        "price": 449.99,
                        "currency": "GBP",
                        "clientRef": "ORD-2026-00918",
                        "registrationStatus": "unassigned",
                    },
                    "customSkuId": "681aa2f1c4b21d0f8c9e0012",
                    "masterSkuId": "681aa2f1c4b21d0f8c9e0044",
                    "skuStatus": "matched",
                    "registeredAt": "2026-08-06T10:14:52.113000Z",
                }
            },
        ),
        404: error("No device with this id.", "Device not found"),
    },
)
def get_device_by_id(device_id: str = Query(..., alias="device_id")):
    """
    Look up a single registered device.

    `device_id` is **mandatory** and is normally the 24-character Mongo `_id` returned by
    `POST /device-register`. A value that is not a valid ObjectId is not an error — it is
    retried as a plain string id, so legacy string-keyed devices still resolve.

    The document is returned as stored, with `_id` converted to a string. Its shape follows the
    registration that created it: `identifiers`, `uniqueParameters` and `registrationParameters`,
    plus the catalogue links (`customSkuId`, `masterSkuId`) resolved at registration time.
    """
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
