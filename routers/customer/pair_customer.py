from fastapi import APIRouter, Body, HTTPException
from pymongo import MongoClient
from bson import ObjectId
import os

from utils.api_docs import error, json_response

router = APIRouter(tags=["Customers"])

# Setup Mongo client and collections
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
customer_collection = db["Customer"]
basket_collection = db["Basket_Quotes"]
devices_collection = db["Devices"]


@router.post(
    "/pair-customer",
    summary="Attach a basket's devices to a customer",
    response_description="Per-device outcome and update counts for both the customer and the devices.",
    responses={
        200: json_response(
            "The pairing ran. **Inspect the counts and `errors`** — a device that could not be "
            "updated is reported here, not raised.",
            {
                "customer_update": {"matched": 1, "modified": 2},
                "customer_device_updates": {"attempted": 2, "matched": 2, "modified": 2, "errors": []},
                "device_update": {"attempted": 2, "matched": 2, "modified": 2, "errors": []},
                "devices": [
                    {"deviceId": "6820f1c9a4b21d0f8c9e4471", "status": "contract"},
                    {"deviceId": "6820f1c9a4b21d0f8c9e4472", "status": "registered"},
                ],
            },
        ),
        400: error("`customer_id` or `basket_id` is not a valid 24-character ObjectId.", "Invalid basket_id"),
        404: error("The basket or the customer does not exist.", "Customer not found"),
    },
)
def pair_customer(
    customer_id: str = Body(..., description="**Mandatory.** The customer to attach the devices to.", examples=["6820f1c9a4b21d0f8c9e9001"]),
    basket_id: str = Body(..., description="**Mandatory.** The basket whose devices are being paired.", examples=["68b2d1f0a4b21d0f8c9e8801"]),
):
    """
    Link every device in a basket to a customer — the step that turns an anonymous basket into
    an owned one, normally run after payment.

    Devices are collected from **both** basket arrays and get a status accordingly:

    - lines in `Basket` → **`contract`** (cover was bought)
    - entries in `skipped_items` → **`registered`** (cover was declined)

    A device in both lists is recorded as `contract`.

    Two writes happen per device: the `Devices` record gets
    `registrationStatus: "assigned"` and the customer's id, and the customer's `devices` array
    gets a `{deviceId, status}` entry, replacing any previous entry for that device. Re-running
    the call is therefore safe — statuses are replaced, not duplicated.

    **Failures are reported, not raised.** A device that cannot be updated appears in the
    relevant `errors` array while the rest still process, and the call returns `200`. Check
    `matched` against `attempted` to confirm everything landed. An empty basket returns zeroed
    counts without touching anything.

    Both ids are mandatory. **This endpoint is not authenticated.**
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
