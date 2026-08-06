from fastapi import APIRouter, HTTPException, Query, Depends, Request
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import os
from dotenv import load_dotenv
from datetime import datetime
import re

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Catalog"]
)

mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    raise RuntimeError("MONGO_URI not set in environment.")

client = MongoClient(mongo_uri)
db = client["Activlink"]
customsku_collection = db["CustomSKU"]
clientkey_collection = db["ClientKey"]
error_log_collection = db["Error_Log_Lookup_Custom_SKU"]
mastersku_collection = db["MasterSKU"]   # <-- Added

def clean_result(result):
    if result is None:
        return None
    # Handles both single dict and list of dicts
    if isinstance(result, dict):
        result = [result]
    for r in result:
        if "_id" in r:
            r["id"] = str(r.pop("_id"))
    return result

# Helper function to lookup MasterSKU by id and locale (internal)
def lookup_mastersku_by_id(mastersku_id, locale):
    if not mastersku_id:
        return None
    try:
        object_id = ObjectId(mastersku_id)
    except Exception:
        return None  # Optionally log error

    query = {
        "_id": object_id,
        "Locale_Specific_Data.locale": locale
    }
    result = mastersku_collection.find_one(query)
    if result:
        result["_id"] = str(result["_id"])
    return result

def attach_master_sku_to_result(result, locale):
    # Handles both single dict and list of dicts
    if isinstance(result, list):
        for doc in result:
            master_sku = lookup_mastersku_by_id(doc.get("MasterSKU"), locale)
            doc["MasterSKU_Details"] = master_sku
    elif isinstance(result, dict):
        master_sku = lookup_mastersku_by_id(result.get("MasterSKU"), locale)
        result["MasterSKU_Details"] = master_sku
    return result

@router.get(
    "/lookup_custom_sku",
    summary="Look up a client's CustomSKU, with its MasterSKU attached",
    response_description="The matching CustomSKU records, which identifier matched, and the linked MasterSKU.",
    responses=secured({
        200: json_response(
            "At least one CustomSKU matched. `matched_by` names the identifier that succeeded.",
            {
                "matched_by": "GTIN",
                "count": 1,
                "results": [
                    {
                        "id": "681aa2f1c4b21d0f8c9e0012",
                        "Client": "ACME-UK",
                        "Identifiers": {"GTIN": "5011773057240", "Make": "Bosch", "Model": "SMS6ZCI00G", "SKU": "BOSCH-DW-4421"},
                        "Locale_Specific_Data": [
                            {"locale": "en_GB", "Title": "Bosch Series 6 Dishwasher", "Category": "Dishwasher", "MSRP": 449.99, "Guarantees": {"Parts": "24", "Labour": "12"}}
                        ],
                        "MasterSKU": "681aa2f1c4b21d0f8c9e0044",
                        "MasterSKU_Details": {
                            "_id": "681aa2f1c4b21d0f8c9e0044",
                            "Make": "Bosch",
                            "Model": "SMS6ZCI00G",
                            "Locale_Specific_Data": [{"locale": "en_GB", "Title": "Bosch Series 6 Dishwasher", "Price": 449.99}],
                        },
                    }
                ],
            },
        ),
        400: error("`id` was supplied but is not a valid 24-character ObjectId.", "Invalid ObjectId format"),
        404: error(
            "The `clientKey` is unknown, or no CustomSKU matched any identifier supplied. "
            "Misses are recorded in `Error_Log_Lookup_Custom_SKU`.",
            "No matching SKU found. Please try again shortly as we update our records.",
        ),
        500: error("The catalogue query failed.", "Database error: ..."),
    }),
)
def lookup_sku(
    clientKey: str = Query(
        ...,
        description="**Mandatory.** Your tenant key. Resolved to a `Client_ID`, which scopes the search.",
        examples=["acme_uk_live"],
    ),
    locale: str = Query(
        ...,
        description="**Mandatory.** Only records carrying `Locale_Specific_Data` for this locale are returned.",
        examples=["en_GB"],
    ),
    Make: Optional[str] = Query(
        None,
        description="Manufacturer. Only used together with `Model`; matched exactly but case-insensitively.",
        examples=["Bosch"],
    ),
    Model: Optional[str] = Query(
        None,
        description="Model designation. Only used together with `Make`; matched as a case-insensitive substring.",
        examples=["SMS6ZCI00G"],
    ),
    GTIN: Optional[str] = Query(None, description="Barcode / GTIN. Tried after `id`.", examples=["5011773057240"]),
    SKU: Optional[str] = Query(None, description="The client's own SKU code. Tried after `GTIN`.", examples=["BOSCH-DW-4421"]),
    id: Optional[str] = Query(None, description="CustomSKU ObjectId. Tried first when supplied.", examples=["681aa2f1c4b21d0f8c9e0012"]),
    _: None = Depends(verify_token)
):
    """
    Find a client's own catalogue record (CustomSKU) for a product, in a given locale.

    `clientKey` and `locale` are **mandatory**. At least one identifier should also be supplied —
    they are tried **in priority order and the first hit wins**:

    1. `id` — the CustomSKU's own ObjectId
    2. `GTIN` — exact match
    3. `SKU` — exact match
    4. `Make` **+** `Model` — case-insensitive; `Make` must match in full, `Model` as a substring.
       Supplying only one of the pair does nothing.

    Each result has its linked MasterSKU resolved for the same locale and attached as
    `MasterSKU_Details` (`null` when the MasterSKU has no data for that locale). `_id` is
    returned as `id`.

    Every miss is written to `Error_Log_Lookup_Custom_SKU` with the parameters that were sent, so
    gaps in the catalogue can be reviewed later. Passing no identifier at all is not a validation
    error — it simply matches nothing and returns `404`.
    """
    # Step 1: Lookup clientKey to get Client_ID and Source
    clientkey_doc = clientkey_collection.find_one({"ClientKey": clientKey})
    if not clientkey_doc or "Client_ID" not in clientkey_doc:
        raise HTTPException(status_code=404, detail="Invalid clientKey")

    client_id = clientkey_doc["Client_ID"]
    source = clientkey_doc.get("Source")   # <-- Captures 'Source'

    # Step 2: Build base query for CustomSKU
    base_query = {
        "Locale_Specific_Data": {"$elemMatch": {"locale": locale}},
        "Client": client_id
    }

    def find_with(extra_query, matched_by):
        full_query = {**base_query, **extra_query}
        try:
            results = list(customsku_collection.find(full_query, {"_id": 0}))
            results = clean_result(results)
        except Exception as e:
            error_log_collection.insert_one({
                "payload": extra_query,
                "status": "exception",
                "message": str(e),
                "timestamp": datetime.utcnow(),
                "source": source
            })
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
        if results:
            attach_master_sku_to_result(results, locale)
            return {
                "matched_by": matched_by,
                "count": len(results),
                "results": results
            }
        return None

    # _id lookup (highest priority)
    if id:
        try:
            object_id = ObjectId(id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ObjectId format")
        full_query = {**base_query, "_id": object_id}
        try:
            result = customsku_collection.find_one(full_query)
            result = clean_result(result)
        except Exception as e:
            error_log_collection.insert_one({
                "payload": {"id": id},
                "status": "exception",
                "message": str(e),
                "timestamp": datetime.utcnow(),
                "source": source
            })
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
        if result:
            attach_master_sku_to_result(result, locale)
            return {
                "matched_by": "_id",
                "count": 1,
                "results": result
            }

    # GTIN lookup
    if GTIN:
        result = find_with({"Identifiers.GTIN": GTIN}, "GTIN")
        if result:
            return result

    # SKU lookup
    if SKU:
        result = find_with({"Identifiers.SKU": SKU}, "SKU")
        if result:
            return result

    # Make + Model (regex, case-insensitive, with input escaping)
    if Make and Model:
        result = find_with({
            "Identifiers.Make": {"$regex": f"^{re.escape(Make)}$", "$options": "i"},
            "Identifiers.Model": {"$regex": re.escape(Model), "$options": "i"}
        }, "Make+Model (fuzzy)")
        if result:
            return result

    # If none matched: log the error (GET-compatible: log only sent params)
    payload = {k: v for k, v in {
        "clientKey": clientKey,
        "locale": locale,
        "source": source,
        "Make": Make,
        "Model": Model,
        "GTIN": GTIN,
        "SKU": SKU,
        "id": id
    }.items() if v is not None}
    error_log_collection.insert_one({
        "payload": payload,
        "status": "error",
        "message": "no customSKU found",
        "timestamp": datetime.utcnow()        
    })

    raise HTTPException(status_code=404, detail="No matching SKU found. Please try again shortly as we update our records.")
