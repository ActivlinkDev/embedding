from fastapi import APIRouter, HTTPException, Query, Depends
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
mastersku_collection = db["MasterSKU"]


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
        return None

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


def filter_locale_specific_data(result, locale):
    """Mutate result(s) to only include Locale_Specific_Data entries where locale matches."""
    def _filter_doc(doc):
        if not isinstance(doc, dict):
            return
        lsd = doc.get("Locale_Specific_Data")
        if isinstance(lsd, list):
            doc["Locale_Specific_Data"] = [e for e in lsd if e and e.get("locale") == locale]
        # Also filter MasterSKU_Details if present
        m = doc.get("MasterSKU_Details")
        if isinstance(m, dict):
            mlsd = m.get("Locale_Specific_Data")
            if isinstance(mlsd, list):
                m["Locale_Specific_Data"] = [e for e in mlsd if e and e.get("locale") == locale]

    if isinstance(result, list):
        for d in result:
            _filter_doc(d)
    elif isinstance(result, dict):
        _filter_doc(result)
    return result


@router.get(
    "/lookup_custom_sku_locale",
    summary="Look up a CustomSKU and return only the requested locale's data",
    response_description="Matching CustomSKUs, with `Locale_Specific_Data` reduced to the requested locale.",
    responses=secured({
        200: json_response(
            "At least one CustomSKU matched. Locale data is filtered down to `locale` on both "
            "the CustomSKU and its attached MasterSKU.",
            {
                "matched_by": "GTIN",
                "count": 1,
                "results": [
                    {
                        "id": "681aa2f1c4b21d0f8c9e0012",
                        "Client": "ACME-UK",
                        "Identifiers": {"GTIN": "5011773057240", "Make": "Bosch", "Model": "SMS6ZCI00G", "SKU": "BOSCH-DW-4421"},
                        "Locale_Specific_Data": [
                            {"locale": "fr_FR", "Title": "Lave-vaisselle Bosch Série 6", "Category": "Lave-vaisselle", "MSRP": 529.0}
                        ],
                        "MasterSKU": "681aa2f1c4b21d0f8c9e0044",
                        "MasterSKU_Details": {
                            "_id": "681aa2f1c4b21d0f8c9e0044",
                            "Make": "Bosch",
                            "Model": "SMS6ZCI00G",
                            "Locale_Specific_Data": [{"locale": "fr_FR", "Title": "Lave-vaisselle Bosch Série 6", "Price": 529.0}],
                        },
                    }
                ],
            },
        ),
        400: error("`id` was supplied but is not a valid 24-character ObjectId.", "Invalid ObjectId format"),
        404: error(
            "The `clientKey` is unknown, or nothing matched. Misses are recorded in "
            "`Error_Log_Lookup_Custom_SKU`.",
            "No matching SKU found. Please try again shortly as we update our records.",
        ),
        500: error("The catalogue query failed.", "Database error: ..."),
    }),
)
def lookup_sku_locale(
    clientKey: str = Query(
        ...,
        description="**Mandatory.** Your tenant key. Resolved to a `Client_ID`, which scopes the search.",
        examples=["acme_uk_live"],
    ),
    locale: str = Query(
        ...,
        description="**Mandatory.** Both the filter for matching and the only locale returned in the results.",
        examples=["fr_FR"],
    ),
    Make: Optional[str] = Query(None, description="Manufacturer. Only used together with `Model`.", examples=["Bosch"]),
    Model: Optional[str] = Query(None, description="Model designation. Only used together with `Make`.", examples=["SMS6ZCI00G"]),
    GTIN: Optional[str] = Query(None, description="Barcode / GTIN. Tried after `id`.", examples=["5011773057240"]),
    SKU: Optional[str] = Query(None, description="The client's own SKU code. Tried after `GTIN`.", examples=["BOSCH-DW-4421"]),
    id: Optional[str] = Query(None, description="CustomSKU ObjectId. Tried first when supplied.", examples=["681aa2f1c4b21d0f8c9e0012"]),
    _: None = Depends(verify_token)
):
    """
    The locale-trimmed variant of `GET /sku/lookup_custom_sku`.

    Matching is identical — `clientKey` and `locale` are mandatory, and the identifiers `id`,
    `GTIN`, `SKU`, then `Make`+`Model` are tried in that order until one hits.

    **The difference is the response.** Each result has its `Locale_Specific_Data` array reduced
    to just the requested locale, on both the CustomSKU and the attached `MasterSKU_Details`, so
    a storefront gets one price and one title rather than every market's. Use the unfiltered
    `lookup_custom_sku` when you need to compare locales.
    """
    # Step 1: Lookup clientKey to get Client_ID and Source
    clientkey_doc = clientkey_collection.find_one({"ClientKey": clientKey})
    if not clientkey_doc or "Client_ID" not in clientkey_doc:
        raise HTTPException(status_code=404, detail="Invalid clientKey")

    client_id = clientkey_doc["Client_ID"]
    source = clientkey_doc.get("Source")

    # Step 2: Build base query for CustomSKU - ensure at least one matching locale exists
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
            filter_locale_specific_data(results, locale)
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
            filter_locale_specific_data(result, locale)
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
