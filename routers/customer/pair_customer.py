from fastapi import APIRouter, Body, HTTPException
from pymongo import MongoClient
from bson import ObjectId
import os

router = APIRouter(tags=["Customer"])

# Setup Mongo client and collections
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
customer_collection = db["Customer"]
basket_collection = db["Basket_Quotes"]
devices_collection = db["Devices"]


@router.post("/pair-customer")
def pair_customer(
    customer_id: str = Body(...),
    basket_id: str = Body(...),
):
    """Attach deviceIds from the basket (Basket and skipped_items) to the Customer document.

    Request body:
      { "customer_id": "<hexid>", "basket_id": "<hexid>" }

    Behavior:
      - Load the basket by _id from Basket_Quotes
      - Collect all deviceId values from `Basket` entries and `skipped_items`
      - Update the Customer document (by _id) adding the deviceIds to a `deviceIds` array
        using $addToSet / $each so duplicates are not created.
      - Return summary with counts and any not-found errors.
    """
    # Validate ids
    try:
        basket_objid = ObjectId(basket_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid basket_id")
    try:
        customer_objid = ObjectId(customer_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid customer_id")

    basket = basket_collection.find_one({"_id": basket_objid})
    if not basket:
        raise HTTPException(status_code=404, detail="Basket not found")

    device_ids = set()
    # map deviceId -> status (contract for Basket items, registered for skipped_items)
    device_status_map: dict = {}

    # Extract from Basket array (status = 'contract')
    for item in basket.get("Basket", []) or []:
        did = item.get("deviceId")
        if did:
            device_ids.add(did)
            # prefer 'contract' if same device appears in both lists
            device_status_map[did] = "contract"

    # Extract from skipped_items (status = 'registered')
    for skip in basket.get("skipped_items", []) or []:
        did = skip.get("deviceId")
        if did:
            device_ids.add(did)
            # only set to 'registered' if not already marked (contract takes precedence)
            device_status_map.setdefault(did, "registered")

    if not device_ids:
        return {"customer_update": {"matched": 0, "modified": 0}, "device_update": {"attempted": 0, "matched": 0, "modified": 0, "errors": []}, "deviceIds": []}

    # Ensure customer exists (we will manage device objects in `devices` array)
    cust_doc = customer_collection.find_one({"_id": customer_objid})
    if not cust_doc:
        raise HTTPException(status_code=404, detail="Customer not found")

    # For each deviceId, update Devices.registrationParameters
    device_update_summary = {
        "attempted": 0,
        "matched": 0,
        "modified": 0,
        "errors": [],
    }

    # track customer-side device status updates
    customer_device_updates = {"attempted": 0, "matched": 0, "modified": 0, "errors": []}

    for did in list(device_ids):
        device_update_summary["attempted"] += 1
        customer_device_updates["attempted"] += 1
        # Try to convert to ObjectId; if it fails, try using the raw string as _id
        oid = None
        try:
            oid = ObjectId(did)
        except Exception:
            oid = None

        query = {"_id": oid} if oid is not None else {"_id": did}
        try:
            dev_res = devices_collection.update_one(
                query,
                {
                    "$set": {
                        "registrationParameters.registrationStatus": "assigned",
                        "registrationParameters.customerId": customer_id,
                    }
                },
            )
            device_update_summary["matched"] += int(dev_res.matched_count)
            device_update_summary["modified"] += int(dev_res.modified_count)
        except Exception as e:
            device_update_summary["errors"].append({"deviceId": did, "error": str(e)})

        # Update the Customer.devices array to reflect the device-specific status
        status = device_status_map.get(did, "registered")
        try:
            # First remove any existing entry for this deviceId
            pull_res = customer_collection.update_one(
                {"_id": customer_objid},
                {"$pull": {"devices": {"deviceId": did}}},
            )
            # Then push the new status object (separate operations avoid Mongo conflict)
            push_res = customer_collection.update_one(
                {"_id": customer_objid},
                {"$push": {"devices": {"deviceId": did, "status": status}}},
            )
            # Count push as the matched indicator for the presence of the customer doc
            customer_device_updates["matched"] += int(push_res.matched_count)
            customer_device_updates["modified"] += int(pull_res.modified_count) + int(push_res.modified_count)
        except Exception as e:
            customer_device_updates["errors"].append({"deviceId": did, "error": str(e)})

    return {
        "customer_update": {"matched": 1, "modified": int(customer_device_updates["modified"])},
        "customer_device_updates": customer_device_updates,
        "device_update": device_update_summary,
        "devices": [ {"deviceId": d, "status": device_status_map.get(d)} for d in list(device_ids) ],
    }
