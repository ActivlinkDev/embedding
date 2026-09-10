"""Pair registration-session devices to a customer when nothing was purchased.

`POST /pair-customer` covers the paid journey: the Stripe webhook hands it a basket and it
marks every device in that basket as belonging to the customer. Journeys that end without a
payment never reach that webhook — a device with no offers, an offer that was declined, or a
zero-value basket — so their `Devices` records stayed `registrationStatus: "unassigned"` with
no `customerId`, even though the customer had been created and verified by OTP.

This endpoint closes that gap for device ids rather than a basket, because the no-offer
journey never creates a basket at all. It is deliberately separate from `/pair-customer`:
that one is unauthenticated and reachable only by the webhook, whereas this one is called on
behalf of a browser, so both the device ids and the phone must be proved by the trusted
frontend before anything is written.
"""
import re

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from utils.mongo import require_client

router = APIRouter(tags=["Customers"])

client = require_client()
db = client["Activlink"]
customer_collection = db["Customer"]
devices_collection = db["Devices"]


class PairCustomerDevicesRequest(BaseModel):
    customer_id: str
    # Supplied by the trusted frontend from its signed, HTTP-only registration cookie —
    # never from browser JSON. The 80 cap mirrors the cookie's own retention limit.
    device_ids: list[str] = Field(min_length=1, max_length=80)
    # Supplied from the verified-OTP cookie. It is what ties this browser to the customer.
    phone: str = Field(pattern=r'^\+[1-9]\d{6,14}$')


def phone_pattern(phone: str) -> dict:
    """Match formatting differences in a stored phone without matching suffixes.

    Same construction as `registration_overview`: anchored at both ends so
    `+447700900123` cannot match `+1447700900123` or a longer local number.
    """
    digits = re.sub(r'\D', '', phone)
    return {'$regex': r'^\+?[\s().-]*' + r'[\s().-]*'.join(digits) + r'[\s().-]*$'}


@router.post(
    "/pair-customer-devices",
    summary="Attach registered devices to a customer without a basket",
    response_description="Per-device outcome and the counts of the device and customer writes.",
    responses=secured({
        200: json_response(
            "The pairing ran. **Inspect `devices`** — a device that was skipped is reported "
            "there with a reason, not raised.",
            {
                "customer_id": "6820f1c9a4b21d0f8c9e9001",
                "device_update": {"attempted": 2, "matched": 2, "modified": 2},
                "devices": [
                    {"deviceId": "6820f1c9a4b21d0f8c9e4471", "status": "registered"},
                    {"deviceId": "6820f1c9a4b21d0f8c9e4472", "status": "skipped",
                     "reason": "Already paired with another customer"},
                ],
            },
        ),
        400: error("`customer_id` or one of `device_ids` is not a valid ObjectId.", "Invalid device identifier"),
        403: error(
            "The customer does not belong to the verified phone, or one of the devices is "
            "claimed by a different phone.",
            "Some registrations are unavailable for this verified phone number",
        ),
        404: error("The customer does not exist.", "Customer not found"),
    }),
)
def pair_customer_devices(body: PairCustomerDevicesRequest, _: None = Depends(verify_token)):
    """
    Mark devices registered in the current session as owned by a customer, for journeys that
    end without a payment.

    Every device gets `registrationParameters.registrationStatus: "assigned"` plus the
    customer's id, and the customer's `devices` array gets a `{deviceId, status}` entry with
    status `registered` — cover was not bought here, so `contract` is never written, and an
    existing `contract` entry from a paid basket is left alone rather than downgraded.

    **Authorization is server-side and fails closed.** The customer must carry the verified
    phone, and every device must either already be claimed by that phone or be unclaimed.
    A device already paired to a *different* customer is skipped and reported, never
    reassigned. Re-running the call is safe: the same values are written again.
    """
    if not ObjectId.is_valid(body.customer_id):
        raise HTTPException(400, 'Invalid customer_id')
    if any(not ObjectId.is_valid(value) for value in body.device_ids):
        raise HTTPException(400, 'Invalid device identifier')

    customer_objid = ObjectId(body.customer_id)
    phone_match = phone_pattern(body.phone)
    # The verified phone is the only thing binding this browser to a customer: a customer_id
    # alone must never be enough to attach someone else's registrations to that customer.
    customer = customer_collection.find_one(
        {'_id': customer_objid,
         '$or': [{field: phone_match} for field in ('telephone', 'phone', 'mobile')]},
        {'devices': 1})
    if not customer:
        # Deliberately not distinguishing "no such customer" from "not your customer": both
        # mean this browser may not write to it, and the difference would confirm ids.
        raise HTTPException(403, 'Customer unavailable for this verified phone number')

    ids = [ObjectId(value) for value in dict.fromkeys(body.device_ids)]
    # A device is claimable when it is unclaimed or already bound to this phone. Anything
    # else means the signed cookie and the OTP disagree about who is registering.
    owner = {'$or': [{'registrationPhone': phone_match}, {'registrationPhone': {'$exists': False}}]}
    if devices_collection.count_documents({'_id': {'$in': ids}, **owner}) != len(ids):
        raise HTTPException(403, 'Some registrations are unavailable for this verified phone number')

    existing_status = {entry.get('deviceId'): entry.get('status')
                       for entry in (customer.get('devices') or []) if isinstance(entry, dict)}

    summary = {'attempted': 0, 'matched': 0, 'modified': 0}
    results = []
    for oid in ids:
        device_id = str(oid)
        summary['attempted'] += 1
        result = devices_collection.update_one(
            {'_id': oid, **owner,
             '$or': [{'registrationParameters.customerId': {'$exists': False}},
                     {'registrationParameters.customerId': body.customer_id}]},
            {'$set': {'registrationParameters.registrationStatus': 'assigned',
                      'registrationParameters.customerId': body.customer_id,
                      'registrationPhone': body.phone}})
        summary['matched'] += int(result.matched_count)
        summary['modified'] += int(result.modified_count)
        if not result.matched_count:
            results.append({'deviceId': device_id, 'status': 'skipped',
                            'reason': 'Already paired with another customer'})
            continue

        # A paid basket may already have recorded this device as `contract`; nothing here
        # bought cover, so that entry stands.
        status = 'contract' if existing_status.get(device_id) == 'contract' else 'registered'
        # Pull then push in separate updates: Mongo rejects both on one array in one update.
        customer_collection.update_one({'_id': customer_objid},
                                       {'$pull': {'devices': {'deviceId': device_id}}})
        customer_collection.update_one({'_id': customer_objid},
                                       {'$push': {'devices': {'deviceId': device_id, 'status': status}}})
        results.append({'deviceId': device_id, 'status': status})

    return {'customer_id': body.customer_id, 'device_update': summary, 'devices': results}
