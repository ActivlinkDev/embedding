from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
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


class DeleteCustomSKURequest(BaseModel):
    ClientKey: str
    id: str = Field(..., description="CustomSKU document id")


@router.post("/delete_custom_sku")
def delete_custom_sku(data: DeleteCustomSKURequest, _: None = Depends(verify_token)):
    """Delete a single CustomSKU by id, scoped to the client that owns it."""
    clientkey_doc = clientkey_collection.find_one({"ClientKey": data.ClientKey})
    if not clientkey_doc or "Client_ID" not in clientkey_doc:
        raise HTTPException(status_code=404, detail="Invalid clientKey")
    client_id = clientkey_doc["Client_ID"]

    try:
        doc_id = ObjectId(data.id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")

    result = customsku_collection.delete_one({"_id": doc_id, "Client": client_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="CustomSKU not found for client")

    return {"message": "CustomSKU deleted", "id": data.id}
