from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import os
from dotenv import load_dotenv

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Catalog"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["CustomSKU"]

@router.get(
    "/lookup_custom_all",
    summary="Look up CustomSKUs across every locale",
    response_description="Matching CustomSKUs with all their locale data, and which identifier matched.",
    responses=secured({
        200: json_response(
            "At least one CustomSKU matched.",
            {
                "matched_by": "GTIN",
                "count": 1,
                "results": [
                    {
                        "Client": "ACME-UK",
                        "Identifiers": {"GTIN": "5011773057240", "Make": "Bosch", "Model": "SMS6ZCI00G", "SKU": "BOSCH-DW-4421"},
                        "Category": "Dishwasher",
                        "MasterSKU": "681aa2f1c4b21d0f8c9e0044",
                        "Locale_Specific_Data": [
                            {"locale": "en_GB", "Title": "Bosch Series 6 Dishwasher", "MSRP": 449.99},
                            {"locale": "fr_FR", "Title": "Lave-vaisselle Bosch Série 6", "MSRP": 529.0},
                        ],
                    }
                ],
            },
        ),
        400: error(
            "`id` is not a valid ObjectId, or a non-`id` search was attempted without `client`.",
            "`client` is required when not searching by _id.",
        ),
        404: error("Nothing matched the parameters supplied.", "No matching SKU found using provided parameters."),
    }),
)
def lookup_sku(
    id: Optional[str] = Query(
        None,
        description="CustomSKU ObjectId. The **only** search that does not need `client`.",
        examples=["681aa2f1c4b21d0f8c9e0012"],
    ),
    client: Optional[str] = Query(
        None,
        description=(
            "`Client_ID` owning the records — **required for every search except by `id`**, and "
            "note this is the Client_ID itself, not a ClientKey."
        ),
        examples=["ACME-UK"],
    ),
    Make: Optional[str] = Query(None, description="Manufacturer. Only used together with `Model`.", examples=["Bosch"]),
    Model: Optional[str] = Query(None, description="Model designation. Only used together with `Make`.", examples=["SMS6ZCI00G"]),
    GTIN: Optional[str] = Query(None, description="Barcode / GTIN, matched exactly.", examples=["5011773057240"]),
    SKU: Optional[str] = Query(None, description="The client's own SKU code, matched exactly.", examples=["BOSCH-DW-4421"]),
    _: None = Depends(verify_token)
):
    """
    Look up CustomSKUs without filtering by locale — every `Locale_Specific_Data` entry is
    returned.

    Identifiers are tried **in order, first hit wins**: `id`, then `GTIN`, then `SKU`, then
    `Make`+`Model` (case-insensitive; `Make` exact, `Model` substring).

    **`client` is mandatory for everything except an `id` lookup**, and it is the raw `Client_ID`
    rather than a ClientKey — unlike `GET /sku/lookup_custom_sku`, which takes a `clientKey` and
    resolves it for you. Omitting it on a GTIN/SKU/Make+Model search returns `400`.

    No MasterSKU is attached to the results here.
    """
    # 1. Try match by _id without client
    if id:
        try:
            object_id = ObjectId(id)
            result = collection.find_one({"_id": object_id}, {"_id": 0})
            if result:
                return {
                    "matched_by": "_id",
                    "count": 1,
                    "results": [result]
                }
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ObjectId format")

    # If any other search is attempted, client becomes required
    if not client:
        raise HTTPException(status_code=400, detail="`client` is required when not searching by _id.")

    base_query = {
        "Client": client
    }

    def find_with(extra_query, matched_by):
        full_query = {**base_query, **extra_query}
        results = list(collection.find(full_query, {"_id": 0}))
        if results:
            return {
                "matched_by": matched_by,
                "count": len(results),
                "results": results
            }
        return None

    if GTIN:
        result = find_with({"Identifiers.GTIN": GTIN}, "GTIN")
        if result:
            return result

    if SKU:
        result = find_with({"Identifiers.SKU": SKU}, "SKU")
        if result:
            return result

    if Make and Model:
        result = find_with({
            "Identifiers.Make": {"$regex": f"^{Make}$", "$options": "i"},
            "Identifiers.Model": {"$regex": Model, "$options": "i"}
        }, "Make+Model (fuzzy)")
        if result:
            return result

    raise HTTPException(status_code=404, detail="No matching SKU found using provided parameters.")
