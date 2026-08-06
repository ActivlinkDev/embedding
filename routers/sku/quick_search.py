from fastapi import APIRouter, HTTPException, Query, Depends
from typing import Optional
from pymongo import MongoClient
import os, re
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
    "/quick_search",
    summary="Search a client's SKUs by free text, for pickers and type-ahead",
    response_description="Matching CustomSKUs, trimmed to the fields a picker needs.",
    responses=secured({
        200: json_response(
            "Matches, up to `limit`. An empty `results` array is a normal outcome, not an error.",
            {
                "count": 2,
                "results": [
                    {
                        "id": "681aa2f1c4b21d0f8c9e0012",
                        "Client": "ACME-UK",
                        "Identifiers": {"GTIN": "5011773057240", "Make": "Bosch", "Model": "SMS6ZCI00G", "SKU": "BOSCH-DW-4421"},
                        "Category": "Dishwasher",
                        "MasterSKU": "681aa2f1c4b21d0f8c9e0044",
                        "Locale_Specific_Data": [{"locale": "en_GB", "Title": "Bosch Series 6 Dishwasher", "MSRP": 449.99}],
                    },
                    {
                        "id": "681aa2f1c4b21d0f8c9e0013",
                        "Client": "ACME-UK",
                        "Identifiers": {"GTIN": "5011773057241", "Make": "Bosch", "Model": "SMS4HCI40G", "SKU": "BOSCH-DW-4422"},
                        "Category": "Dishwasher",
                        "MasterSKU": "681aa2f1c4b21d0f8c9e0045",
                        "Locale_Specific_Data": [{"locale": "en_GB", "Title": "Bosch Series 4 Dishwasher", "MSRP": 379.99}],
                    },
                ],
            },
        ),
        400: error("`q` is shorter than two characters and `mode` is not `all`.", "q must be at least 2 characters unless mode=all"),
        404: error("The `clientKey` is unknown.", "Invalid clientKey"),
    }),
)
def quick_search(
    clientKey: str = Query(
        ...,
        description="**Mandatory.** Tenant key; resolved to a `Client_ID` that scopes every result.",
        examples=["acme_uk_live"],
    ),
    q: Optional[str] = Query(
        None,
        description=(
            "Free text, matched as a case-insensitive substring against GTIN, SKU, Make and "
            "Model. **At least two characters** unless `mode=all`."
        ),
        examples=["bosch"],
    ),
    mode: Optional[str] = Query(
        None,
        description="Set to `all` to browse the client's catalogue without a query. Ignored when `q` is supplied.",
        examples=["all"],
    ),
    locale: Optional[str] = Query(
        None,
        description=(
            "Restrict to SKUs carrying this locale, and return only that locale's entry in "
            "`Locale_Specific_Data`. Omit to search every locale and return them all."
        ),
        examples=["en_GB"],
    ),
    limit: int = Query(20, ge=1, le=500, description="Maximum results, between 1 and 500.", examples=[20]),
    _: None = Depends(verify_token)
):
    """
    Search one client's SKUs by free text — built for type-ahead pickers rather than exact lookup.

    `clientKey` is **mandatory** and scopes every result to that tenant. Then either:

    - pass **`q`** (two characters or more) to substring-match GTIN, SKU, Make or Model, or
    - pass **`mode=all`** with no `q` to browse the whole catalogue, still capped by `limit`.

    Supplying neither is rejected with `400`. Supplying both means `q` wins — `mode` is ignored
    whenever a query is present.

    Results are **trimmed to picker fields** (identifiers, category, MasterSKU link and locale
    data), not the full document — fetch that with `GET /sku/get_custom_sku`. Finding nothing is
    a `200` with `count: 0`.
    """
    # Resolve clientKey -> Client_ID; this validates the caller holds a real key for the tenant.
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
            {"Identifiers.GTIN": {"$regex": safe, "$options": "i"}},
        ]
        query = {"$and": [base, {"$or": or_conditions}]}

    projection = {
        "Client": 1,
        "Identifiers": 1,
        "Category": 1,
        "Global_Promotion": 1,
        "MasterSKU": 1,
        "Locale_Specific_Data": {"$elemMatch": {"locale": locale}} if locale else 1,
    }

    results = list(customsku_collection.find(query, projection).limit(int(limit)))
    items = [_to_id_str(r) for r in results]

    return {
        "count": len(items),
        "results": items,
    }
