from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import os
from dotenv import load_dotenv

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token  # ✅ Token-based auth

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Catalog"],
    dependencies=[Depends(verify_token)]  # ✅ Apply token auth to all routes in this router
)

# MongoDB connection
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["MasterSKU"]

@router.get(
    "/lookup_master_sku_all",
    summary="Search the shared MasterSKU catalogue",
    response_description="The single best-matching MasterSKU and which identifier found it.",
    responses=secured({
        200: json_response(
            "One MasterSKU matched. `matched_by` names the identifier that succeeded.",
            {
                "matched_by": "GTIN",
                "result": {
                    "_id": "681aa2f1c4b21d0f8c9e0044",
                    "Make": "Bosch",
                    "Model": "SMS6ZCI00G",
                    "GTIN": ["5011773057240"],
                    "Locale_Specific_Data": [
                        {"locale": "en_GB", "Title": "Bosch Series 6 Dishwasher", "Price": 449.99}
                    ],
                },
            },
        ),
        400: error("`id` was supplied but is not a valid 24-character ObjectId.", "Invalid ObjectId format"),
        404: error("Nothing matched any of the identifiers supplied.", "No matching MasterSKU found"),
    }),
)
def lookup_master_sku(
    id: Optional[str] = Query(None, description="MasterSKU ObjectId. Tried first.", examples=["681aa2f1c4b21d0f8c9e0044"]),
    GTIN: Optional[str] = Query(
        None,
        description="Barcode / GTIN. Matched against the document's GTIN list, so one of several barcodes still hits.",
        examples=["5011773057240"],
    ),
    Make: Optional[str] = Query(None, description="Manufacturer. Only used together with `Model`.", examples=["Bosch"]),
    Model: Optional[str] = Query(None, description="Model designation. Only used together with `Make`.", examples=["SMS6ZCI00G"]),
):
    """
    Search the shared MasterSKU catalogue across every locale.

    All parameters are optional, but at least one identifier is needed to match anything. They
    are tried **in order, first hit wins**, and **only one** record is ever returned:

    1. `id` — the MasterSKU's ObjectId
    2. `GTIN` — matched against the document's GTIN array
    3. `Make` **+** `Model` — `Make` case-insensitive exact, `Model` case-insensitive substring.
       One without the other is ignored.

    Unlike `GET /sku/lookup_master_sku`, this does **not** filter by locale — the whole document
    is returned with every `Locale_Specific_Data` entry it has.
    """
    # 1. Match by MongoDB ObjectId
    if id:
        try:
            object_id = ObjectId(id)
            result = collection.find_one({"_id": object_id})
            if result:
                result["_id"] = str(result["_id"])
                return {"matched_by": "_id", "result": result}
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ObjectId format")

    # 2. Match by GTIN (supports GTIN as array or single value)
    if GTIN:
        result = collection.find_one({"GTIN": {"$in": [GTIN]}})
        if result:
            result["_id"] = str(result["_id"])
            return {"matched_by": "GTIN", "result": result}

    # 3. Match by Make & Model (case-insensitive)
    if Make and Model:
        result = collection.find_one({
            "Make": {"$regex": f"^{Make}$", "$options": "i"},
            "Model": {"$regex": Model, "$options": "i"}
        })
        if result:
            result["_id"] = str(result["_id"])
            return {"matched_by": "Make+Model", "result": result}

    raise HTTPException(status_code=404, detail="No matching MasterSKU found")
