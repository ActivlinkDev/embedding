# routers/client_lookup.py

from fastapi import APIRouter, HTTPException, Query, Depends
from pymongo import MongoClient
import os
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

router = APIRouter(tags=["Catalog"])

# MongoDB connection
MONGO_URI = os.getenv("MONGO_URI", "your-default-mongo-uri")
client = MongoClient(MONGO_URI)
db = client["Activlink"]
collection = db["ClientKey"]

@router.get(
    "/get-client",
    summary="Fetch a client record by its ClientKey",
    response_description="The stored ClientKey document, without its Mongo `_id`.",
    responses=secured({
        200: json_response(
            "The client was found. Fields vary by tenant — `Styles` in particular is free-form "
            "and may be absent.",
            {
                "ClientKey": "acme_uk_live",
                "Client_ID": "ACME-UK",
                "Client_Name": "Acme Retail UK",
                "Source": "web",
                "Styles": {"primaryColour": "#0B5FFF", "logoUrl": "https://cdn.example.com/acme.svg"},
            },
        ),
        404: error("No client is registered under this ClientKey.", "Client not found for the given clientkey"),
    }),
)
def get_client(
    clientkey: str = Query(
        ...,
        description="**Mandatory.** The tenant key whose full client record should be returned.",
        examples=["acme_uk_live"],
    ),
    _: None = Depends(verify_token),
):
    """
    Look up the full client record behind a `ClientKey`.

    This is how you resolve a tenant key into its `Client_ID` (the value the assignment and
    rating endpoints expect) along with the client's channel `Source` and any stored branding
    `Styles`.

    The document is returned as stored, minus `_id`, so the exact field set depends on how the
    tenant was configured. Callers pinned to a different tenant receive `403` from the auth layer
    before this endpoint runs.
    """
    result = collection.find_one({"ClientKey": clientkey}, {"_id": 0})

    if result:
        return result
    else:
        raise HTTPException(status_code=404, detail="Client not found for the given clientkey")
