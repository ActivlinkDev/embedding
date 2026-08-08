from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
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


class DeleteCustomSKURequest(BaseModel):
    """Identifies the record to delete. Both fields are mandatory and must agree."""

    ClientKey: str = Field(
        ...,
        description="**Mandatory.** Tenant key; the record must belong to the client it resolves to.",
        examples=["acme_uk_live"],
    )
    id: str = Field(
        ...,
        description="**Mandatory.** CustomSKU document id (24-character ObjectId).",
        examples=["681aa2f1c4b21d0f8c9e0012"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {"ClientKey": "acme_uk_live", "id": "681aa2f1c4b21d0f8c9e0012"}
        }
    }


@router.post(
    "/delete_custom_sku",
    summary="Delete a CustomSKU",
    response_description="Confirmation naming the deleted record.",
    responses=secured({
        200: json_response(
            "The record was deleted.",
            {"message": "CustomSKU deleted", "id": "681aa2f1c4b21d0f8c9e0012"},
        ),
        400: error("`id` is not a valid 24-character ObjectId.", "Invalid id"),
        404: error(
            "The `ClientKey` is unknown, or no such CustomSKU exists **for that client**.",
            "CustomSKU not found for client",
        ),
    }),
)
def delete_custom_sku(data: DeleteCustomSKURequest, _: None = Depends(verify_token)):
    """
    Permanently delete one CustomSKU.

    Note this is a **`POST`**, not a `DELETE` — the id travels in the body alongside the tenant
    key. Both fields are mandatory and the delete only matches when both agree, so a record owned
    by another client cannot be removed; that case returns `404`, indistinguishable from a record
    that never existed.

    The deletion is immediate and not reversible. Devices already registered against this SKU
    keep their stored `customSkuId`, which will no longer resolve. Re-running the call for an
    already-deleted record returns `404`.
    """
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
