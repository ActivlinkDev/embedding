from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, HTTPException
import os

from utils.api_docs import error, json_response
from utils.mongo import require_client

router = APIRouter(tags=["Customers"])

# Setup Mongo client and collection
client = require_client()
db = client["Activlink"]
customer_collection = db["Customer"]
clientkey_collection = db["ClientKey"]

# Marketing consent is recorded per channel. Anything outside this tuple is dropped rather
# than stored, so a caller cannot invent channels on the customer record.
MARKETING_CHANNELS = ("email", "sms", "phone", "post")


def normalise_marketing_preferences(raw: Any) -> Optional[Dict[str, bool]]:
    """Coerce an incoming preferences payload into `{channel: bool}` for known channels only.

    Returns None when nothing usable was supplied, which callers treat as "leave whatever is
    already stored alone" — an absent payload is not the same as opting out of everything.
    Every known channel is always present in the result, so a channel the caller omitted is
    recorded as an explicit False rather than being left ambiguous.
    """
    if not isinstance(raw, dict):
        return None
    return {channel: bool(raw.get(channel)) for channel in MARKETING_CHANNELS}


def upsert_marketing_preferences(
    collection,
    customer_id: str,
    client_key: str,
    channels: Dict[str, bool],
) -> None:
    """Store `channels` against `client_key` in the customer's `marketingPreferences` array.

    A customer can be reached through several clients, each holding its own consent, so the
    array carries one entry per ClientKey and only the entry for `client_key` is touched —
    another client's consent is never read, overwritten or removed. An entry that already
    exists is replaced with the values just supplied, which is the "latest wins" rule the
    registration journey needs when a returning customer re-submits the form.

    The replace-then-append pair avoids a read-modify-write: each step is a single atomic
    update guarded so it only applies in the state it expects, so two concurrent requests for
    the same (customer, client) cannot produce a duplicate entry for that ClientKey.
    """
    entry = {
        "clientKey": client_key,
        "channels": channels,
        "updatedAt": datetime.now(timezone.utc),
    }

    # Two passes: the append can lose its race to a concurrent request, in which case the
    # entry now exists and the replace on the next pass is the one that applies.
    for _ in range(2):
        replaced = collection.update_one(
            {"_id": ObjectId(customer_id), "marketingPreferences.clientKey": client_key},
            {"$set": {"marketingPreferences.$": entry}},
        )
        if replaced.matched_count:
            return

        appended = collection.update_one(
            {"_id": ObjectId(customer_id), "marketingPreferences.clientKey": {"$ne": client_key}},
            {"$push": {"marketingPreferences": entry}},
        )
        if appended.matched_count:
            return

# --- Reusable Function ---
def get_or_create_customer(
    collection,
    name: str,
    telephone: str,
    email: str
) -> (str, bool):
    """
    Checks if a customer exists by telephone or email (case-insensitive).
    Returns (customer_id, existing: bool).
    If not found, creates and returns new id.
    """
    query = {
        "$or": [
            {"telephone": telephone},
            {"email": {"$regex": f"^{email}$", "$options": "i"}}
        ]
    }
    existing = collection.find_one(query)
    if existing:
        return str(existing["_id"]), True
    customer_doc = {"name": name, "telephone": telephone, "email": email}
    result = collection.insert_one(customer_doc)
    return str(result.inserted_id), False

# --- FastAPI Endpoint using the function ---
@router.post(
    "/get-or-create-customer",
    summary="Find a customer by phone or email, creating one if needed",
    response_description="The customer id, and whether they already existed.",
    responses={
        200: json_response(
            "A customer id, either matched or newly created. `existing` tells you which.",
            {"customerId": "6820f1c9a4b21d0f8c9e9001", "existing": False, "marketingPreferencesStored": True},
        ),
        400: error("`clientKey` was supplied but is not a known tenant.", "Invalid clientKey."),
    },
)
def get_or_create_customer_endpoint(
    name: str = Body(..., description="**Mandatory.** Customer's full name. Only used when creating a new record.", examples=["Jane Okafor"]),
    telephone: str = Body(..., description="**Mandatory.** Phone number, matched **exactly** as given.", examples=["+447700900123"]),
    email: str = Body(..., description="**Mandatory.** Email address, matched case-insensitively.", examples=["jane.okafor@example.com"]),
    clientKey: Optional[str] = Body(
        None,
        description=(
            "Optional. The tenant the customer is registering through. Required in order to store "
            "`marketingPreferences`, since consent is held per client. Must be a known ClientKey."
        ),
        examples=["acme_uk_live"],
    ),
    marketingPreferences: Optional[Dict[str, bool]] = Body(
        None,
        description=(
            "Optional. Per-channel marketing opt-in for this client, as `{email, sms, phone, post}`. "
            "Omitted channels are recorded as `false`; unknown channels are ignored. Stored against "
            "`clientKey`, replacing any consent previously recorded for that client. Omit the field "
            "entirely to leave stored consent untouched."
        ),
        examples=[{"email": True, "sms": False, "phone": False, "post": False}],
    ),
):
    """
    Look up a customer by phone **or** email, and create one if neither matches.

    All three fields are mandatory. Matching is an **OR**: a record whose `telephone` matches
    exactly, or whose `email` matches case-insensitively, is returned as-is. Note that phone
    matching is a literal string comparison — `+447700900123` and `07700 900123` are treated as
    different people, so normalise to E.164 before calling.

    `existing: true` means an existing record was returned and **nothing was updated** — a
    changed name or email on a matched customer is silently ignored. `existing: false` means a
    new customer was created from all three fields.

    **Marketing preferences are the exception**: supply `clientKey` and `marketingPreferences`
    together and the per-channel consent is written for that client whether the customer was
    matched or created, replacing whatever that client had recorded before. Consent is held per
    client in the `marketingPreferences` array — one entry per ClientKey — so a customer reached
    through several clients keeps a separate, independent choice for each, and writing one never
    reads or disturbs another. `marketingPreferencesStored` in the response says whether an entry
    was written. Omit `marketingPreferences` to leave stored consent alone; sending it with every
    channel false is a positive opt-out and is stored as such.

    An unknown `clientKey` is rejected with `400` rather than stored, so consent cannot be filed
    under a tenant that does not exist. `marketingPreferences` without a `clientKey` is ignored,
    since there would be no client to attribute the consent to.

    **This endpoint is not authenticated.** It is called during checkout, including from the
    Stripe webhook. Only the id is returned, never the customer record.
    """
    client_key = (clientKey or "").strip()
    if client_key and not clientkey_collection.find_one({"ClientKey": client_key}):
        raise HTTPException(status_code=400, detail="Invalid clientKey.")

    customer_id, existing = get_or_create_customer(
        customer_collection, name, telephone, email
    )

    channels = normalise_marketing_preferences(marketingPreferences)
    stored = False
    if client_key and channels is not None:
        upsert_marketing_preferences(customer_collection, customer_id, client_key, channels)
        stored = True

    return {"customerId": customer_id, "existing": existing, "marketingPreferencesStored": stored}

# --- You can use the function elsewhere in the file too ---
def use_customer():
    cid, exists = get_or_create_customer(
        customer_collection, "Bob", "5550000", "bob@example.com"
    )
    print("CustomerID:", cid, "| Exists:", exists)
