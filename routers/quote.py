from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import os
from utils.api_docs import error, json_response, secured
from utils.dependencies import caller_client_key, verify_token

router = APIRouter(tags=["Quotes"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
quotes_collection = db["Quotes"]


def _serialize_quote(doc):
    if not doc:
        return None
    out = dict(doc)
    _id = out.get("_id")
    if _id is not None:
        out["_id"] = str(_id)
    ca = out.get("created_at")
    if isinstance(ca, datetime):
        out["created_at"] = ca.isoformat()
    return out


@router.get(
    "/quote/{quote_id}",
    summary="Retrieve a stored quote",
    response_description="The quote as stored, with its id and timestamp as strings.",
    responses=secured({
        200: json_response(
            "The quote was found and belongs to the caller's tenant.",
            {
                "_id": "68a1c2d3e4b21d0f8c9e7712",
                "deviceId": "6820f1c9a4b21d0f8c9e4471",
                "clientKey": "acme_uk_live",
                "created_at": "2026-08-06T10:14:52.113000",
                "responses": [
                    {
                        "product_id": "ACME-EW-STD",
                        "client": "ACME-UK",
                        "currency": "GBP",
                        "locale": "en_GB",
                        "category": "Dishwasher",
                        "age": 15,
                        "price": 449.99,
                        "multi_count": 1,
                        "source": "web",
                        "options": [
                            {"status": "ok", "poc": 24, "mode": "live", "rate": 71.34, "rounded_price": 71.49, "rounded_price_pence": 7149}
                        ],
                    }
                ],
            },
        ),
        400: error("`quote_id` is not a valid 24-character ObjectId.", "Invalid quote_id; must be a valid ObjectId string"),
        404: error(
            "No such quote — **or** it belongs to another tenant. The two are deliberately "
            "indistinguishable.",
            "Quote not found",
        ),
    }),
)
def get_quote(
    quote_id: str,
    _: None = Depends(verify_token),
    scope: str | None = Depends(caller_client_key),
):
    """
    Fetch a quote previously created by `POST /rate_request`, `POST /embedded_quote` or
    `POST /widget_quote`.

    Path parameter `quote_id` is mandatory and is the `quote_id` those endpoints returned.

    **Tenant-scoped.** A caller pinned to a ClientKey only sees quotes stored under that key;
    anything else returns `404` rather than `403`, so a pinned caller cannot discover that
    another tenant's quote id exists. Callers holding `clientkey:*` see any quote.

    The document is returned as stored — `responses` is the same grouped structure
    `/rate_request` produced, so prices do **not** get re-calculated on read. A quote is a
    record of what was priced at the time, not a live valuation.
    """
    try:
        qid = ObjectId(quote_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid quote_id; must be a valid ObjectId string")

    # store_quote() writes the owner as camelCase clientKey, unlike contracts/orders.
    q = {"_id": qid}
    if scope:
        q["clientKey"] = scope

    doc = quotes_collection.find_one(q)
    if not doc:
        # 404 rather than 403: a pinned caller must not learn that another tenant's id exists.
        raise HTTPException(status_code=404, detail="Quote not found")
    return _serialize_quote(doc)
