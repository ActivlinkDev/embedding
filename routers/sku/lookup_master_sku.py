from fastapi import APIRouter, HTTPException, Query, Depends
from bson import ObjectId
from pymongo import MongoClient
import os
from dotenv import load_dotenv

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token  # Token-based auth

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Catalog"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["MasterSKU"]

@router.get(
    "/lookup_master_sku",
    summary="Fetch a MasterSKU by id, for one locale",
    response_description="The MasterSKU document with its `_id` as a string.",
    responses=secured({
        200: json_response(
            "The MasterSKU exists and carries data for this locale.",
            {
                "_id": "681aa2f1c4b21d0f8c9e0044",
                "Make": "Bosch",
                "Model": "SMS6ZCI00G",
                "GTIN": ["5011773057240"],
                "Productname": "BOSCH-DW-4421",
                "Locale_Specific_Data": [
                    {"locale": "en_GB", "Title": "Bosch Series 6 Dishwasher", "Category": "Dishwasher", "Price": 449.99}
                ],
            },
        ),
        400: error("`id` is not a valid 24-character ObjectId.", "Invalid ObjectId format"),
        404: error(
            "No MasterSKU with that id **carrying data for that locale** — the id alone is not "
            "enough to match.",
            "No matching MasterSKU found for the given ID and locale",
        ),
    }),
)
def lookup_master_sku(
    id: str = Query(
        ...,
        description="**Mandatory.** The `_id` of the MasterSKU document.",
        examples=["681aa2f1c4b21d0f8c9e0044"],
    ),
    locale: str = Query(
        ...,
        description="**Mandatory.** The document must contain `Locale_Specific_Data` for this locale, or it does not match.",
        examples=["en_GB"],
    ),
    _: None = Depends(verify_token)
):
    """
    Fetch a single shared catalogue record (MasterSKU) by id, requiring data for one locale.

    A MasterSKU is the platform-wide product record that client-specific CustomSKUs point at.
    Both parameters are mandatory and both are part of the match: an id that exists but has no
    `Locale_Specific_Data` entry for `locale` returns `404`, not a partial document.

    To find a MasterSKU without knowing its id, use `GET /sku/lookup_master_sku_all`, which
    searches by GTIN or make and model and does not filter by locale.
    """
    try:
        object_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ObjectId format")

    query = {
        "_id": object_id,
        "Locale_Specific_Data.locale": locale
    }

  

    result = collection.find_one(query)

    if result:
        # Convert ObjectId to string
        result["_id"] = str(result["_id"])
        return result

    raise HTTPException(status_code=404, detail="No MasterSKU found for given ID and locale")
