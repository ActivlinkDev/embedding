from fastapi import APIRouter, HTTPException, Query, Depends
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

mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    raise RuntimeError("MONGO_URI not set in environment.")

client = MongoClient(mongo_uri)
db = client["Activlink"]
customsku_collection = db["CustomSKU"]
clientkey_collection = db["ClientKey"]


def _to_id_str(doc):
    if not doc:
        return doc
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    return doc


@router.get(
    "/get_custom_sku",
    summary="Fetch one CustomSKU in full, for editing",
    response_description="The complete CustomSKU document, all locales included.",
    responses=secured({
        200: json_response(
            "The document, with `_id` renamed to `id`.",
            {
                "customsku": {
                    "id": "681aa2f1c4b21d0f8c9e0012",
                    "Client": "ACME-UK",
                    "Identifiers": {"GTIN": "5011773057240", "Make": "Bosch", "Model": "SMS6ZCI00G", "SKU": "BOSCH-DW-4421"},
                    "Category": "Dishwasher",
                    "MasterSKU": "681aa2f1c4b21d0f8c9e0044",
                    "Locale_Specific_Data": [
                        {"locale": "en_GB", "Title": "Bosch Series 6 Dishwasher", "MSRP": 449.99, "Guarantees": {"Parts": "24", "Labour": "12"}},
                        {"locale": "fr_FR", "Title": "Lave-vaisselle Bosch Série 6", "MSRP": 529.0},
                    ],
                }
            },
        ),
        400: error("`id` is not a valid 24-character ObjectId.", "Invalid id"),
        404: error(
            "The `clientKey` is unknown, or the CustomSKU does not exist **for that client**.",
            "CustomSKU not found for client",
        ),
    }),
)
def get_custom_sku(
    id: str = Query(..., description="**Mandatory.** The CustomSKU's document id.", examples=["681aa2f1c4b21d0f8c9e0012"]),
    clientKey: str = Query(
        ...,
        description="**Mandatory.** Tenant key; resolved to a `Client_ID` which the document must belong to.",
        examples=["acme_uk_live"],
    ),
    _: None = Depends(verify_token),
):
    """
    Fetch one CustomSKU in full — every locale and every root field — for editing.

    This is the read half of the portal's edit page: load with this, then send the whole document
    back through `POST /sku/update_custom_sku`. Unlike `GET /sku/lookup_custom_sku`, nothing is
    filtered by locale and no MasterSKU is attached; you get the record exactly as stored, with
    `_id` renamed to `id`.

    Both parameters are mandatory and both must agree — a document owned by another client
    returns `404`, the same as one that does not exist, so ids cannot be probed across tenants.
    """
    clientkey_doc = clientkey_collection.find_one({"ClientKey": clientKey})
    if not clientkey_doc or "Client_ID" not in clientkey_doc:
        raise HTTPException(status_code=404, detail="Invalid clientKey")
    client_id = clientkey_doc["Client_ID"]

    try:
        doc_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")

    doc = customsku_collection.find_one({"_id": doc_id, "Client": client_id})
    if not doc:
        raise HTTPException(status_code=404, detail="CustomSKU not found for client")

    return {"customsku": _to_id_str(doc)}
