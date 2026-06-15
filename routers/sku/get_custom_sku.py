from fastapi import APIRouter, HTTPException, Query, Depends
from pymongo import MongoClient
from bson import ObjectId
import os
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


@router.get("/get_custom_sku")
def get_custom_sku(
    id: str = Query(..., description="CustomSKU document id"),
    clientKey: str = Query(..., description="Client key — resolves to a Client_ID internally"),
    _: None = Depends(verify_token),
):
    """Fetch a single CustomSKU by id, scoped to the client that owns it.

    Used by the admin portal edit page to load the full, current document
    (all locales + root fields) before editing.
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
