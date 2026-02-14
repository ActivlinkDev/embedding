from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Optional
from pymongo import MongoClient
import os, re
from dotenv import load_dotenv

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


@router.get("/quick_search")
def quick_search(
    clientKey: str = Query(..., description="Your assigned client key (required)"),
    q: Optional[str] = Query(None, description="Free-text query matching GTIN, SKU, Make, or Model"),
    mode: Optional[str] = Query(None, description="Use mode=all to return all client SKUs (subject to limit)."),
    locale: Optional[str] = Query(None, description="Optional locale to require presence in Locale_Specific_Data"),
    limit: int = Query(20, ge=1, le=500, description="Max results to return"),
    _: None = Depends(verify_token)
):
    # Resolve clientKey -> Client_ID
    clientkey_doc = clientkey_collection.find_one({"ClientKey": clientKey})
    if not clientkey_doc or "Client_ID" not in clientkey_doc:
        raise HTTPException(status_code=404, detail="Invalid clientKey")
    client_id = clientkey_doc["Client_ID"]

    base = {"Client": client_id}
    if locale:
        base["Locale_Specific_Data.locale"] = locale

    mode_normalized = (mode or "").strip().lower()
    query = base

    if mode_normalized != "all" or q:
        search_term = (q or "").strip()
        if len(search_term) < 2:
            raise HTTPException(status_code=400, detail="q must be at least 2 characters unless mode=all")

        safe = re.escape(search_term)
        or_conditions = [
            {"Identifiers.Make": {"$regex": safe, "$options": "i"}},
            {"Identifiers.Model": {"$regex": safe, "$options": "i"}},
            {"Identifiers.SKU": {"$regex": safe, "$options": "i"}},
            {"Identifiers.GTIN": {"$regex": safe, "$options": "i"}},  # works when GTIN is array of strings
        ]
        query = {"$and": [base, {"$or": or_conditions}]}

    # Projection: limit payload size; include one matching locale element if provided
    projection = {
        "Client": 1,
        "Identifiers": 1,
        "Category": 1,
        "MasterSKU": 1,
        "Locale_Specific_Data": {"$elemMatch": {"locale": locale}} if locale else 1,
    }

    results = list(customsku_collection.find(query, projection).limit(int(limit)))
    items = [_to_id_str(r) for r in results]

    return {
        "count": len(items),
        "results": items,
    }
