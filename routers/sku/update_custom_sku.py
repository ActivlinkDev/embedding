from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional
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


class LocaleDetailsPatch(BaseModel):
    """Per-locale fields to change.

    Three states per field, and the difference matters:
    **omit** it to leave the stored value alone, send a **value** to overwrite it, or send
    **`null`** to clear it from the document entirely.
    """

    Title: Optional[str] = Field(None, description="Display title for this locale. `null` clears it.", examples=["Bosch Series 6 Dishwasher"])
    Price: Optional[float] = Field(None, description="Selling price — stored as `MSRP`. `null` clears it.", examples=[449.99])
    GTL: Optional[int] = Field(None, description="Guarantee, labour, in months. `null` clears it.", examples=[12])
    GTP: Optional[int] = Field(None, description="Guarantee, parts, in months. `null` clears it.", examples=[24])
    Promo_Code: Optional[str] = Field(None, description="Promotion for this locale. `null` clears it.", examples=["SUMMER25"])


class UpdateCustomSKURequest(BaseModel):
    """A partial update. `ClientKey` and `id` identify the record; **at least one** other field
    must be present or the request is rejected with `400`."""

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
    SKU: Optional[str] = Field(
        None,
        description="New SKU code. Must be non-blank and unique for this client, else `400`/`409`.",
        examples=["BOSCH-DW-4421"],
    )
    Category: Optional[str] = Field(None, description="New root category.", examples=["Dishwasher"])
    Global_Promotion: Optional[str] = Field(None, description="New promotion applied across every locale.", examples=["LAUNCH10"])
    Locale: Optional[str] = Field(
        None,
        description=(
            "Which locale `Locale_Details` applies to. **Mandatory whenever `Locale_Details` is "
            "sent**, and the locale must already exist on the record."
        ),
        examples=["en_GB"],
    )
    Locale_Details: Optional[LocaleDetailsPatch] = Field(None, description="Per-locale fields to change. Requires `Locale`.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "ClientKey": "acme_uk_live",
                "id": "681aa2f1c4b21d0f8c9e0012",
                "Category": "Dishwasher",
                "Locale": "en_GB",
                "Locale_Details": {"Price": 429.99, "GTP": 24},
            }
        }
    }


def _to_id_str(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return doc
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


@router.post(
    "/update_custom_sku",
    summary="Update a CustomSKU's root or per-locale fields",
    response_description="Confirmation plus the CustomSKU as it now stands.",
    responses=secured({
        200: json_response(
            "The record was updated.",
            {
                "message": "CustomSKU updated",
                "customsku": {
                    "_id": "681aa2f1c4b21d0f8c9e0012",
                    "Client": "ACME-UK",
                    "Identifiers": {"GTIN": ["5011773057240"], "Make": "Bosch", "Model": "SMS6ZCI00G", "SKU": "BOSCH-DW-4421"},
                    "Category": "Dishwasher",
                    "MasterSKU": "681aa2f1c4b21d0f8c9e0044",
                    "Locale_Specific_Data": [
                        {"locale": "en_GB", "Title": "Bosch Series 6 Dishwasher", "MSRP": 429.99, "Guarantees": {"Parts": 24, "Labour": 12}}
                    ],
                },
            },
        ),
        400: error(
            "Nothing to update, a blank `SKU`, `Locale_Details` without `Locale`, or a "
            "malformed `id`.",
            "Locale is required when Locale_Details is provided",
        ),
        404: error(
            "Unknown `ClientKey`, no such record for this client, or the `Locale` is not on the record.",
            "Locale en_GB not found on CustomSKU",
        ),
        409: error("Another CustomSKU of this client already uses that SKU code.", "SKU already exists for this client"),
        500: error("The update was written but the record could not be re-read.", "Failed to load updated CustomSKU"),
    }),
)
def update_custom_sku(data: UpdateCustomSKURequest, background_tasks: BackgroundTasks, _: None = Depends(verify_token)):
    """
    Update an existing CustomSKU — root fields, one locale's details, or both in one call.

    Note this is a **`POST`**, not a `PATCH`, but it behaves as a partial update: only the fields
    you send are touched. Beyond `ClientKey` and `id`, **at least one updatable field is
    required**; a request that changes nothing is rejected with `400`.

    **Clearing versus leaving alone.** Inside `Locale_Details`, an omitted field is left as it is,
    while a field sent explicitly as `null` is removed from the document. That distinction is the
    only way to delete a stored value.

    **Locale rules.** `Locale_Details` requires `Locale`, and that locale must already exist on
    the record — this endpoint edits locales, it does not add them. Use
    `POST /sku/create_custom_sku` with the new locale for that.

    Changing `SKU` is checked for uniqueness within the client and returns `409` on a clash. The
    record must belong to the client behind `ClientKey`; otherwise `404`, the same answer as a
    record that does not exist. After a successful write the widget quote cache is refreshed in
    the background for every affected locale, since price and guarantee changes alter quotes.
    """
    clientkey_doc = clientkey_collection.find_one({"ClientKey": data.ClientKey})
    if not clientkey_doc or "Client_ID" not in clientkey_doc:
        raise HTTPException(status_code=404, detail="Invalid clientKey")
    client_id = clientkey_doc["Client_ID"]

    try:
        doc_id = ObjectId(data.id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")

    existing = customsku_collection.find_one({"_id": doc_id, "Client": client_id})
    if not existing:
        raise HTTPException(status_code=404, detail="CustomSKU not found for client")

    if data.Locale_Details and not data.Locale:
        raise HTTPException(status_code=400, detail="Locale is required when Locale_Details is provided")

    set_ops = {}
    unset_ops = {}
    update_kwargs = {}

    if data.SKU is not None:
        new_sku = data.SKU.strip()
        if not new_sku:
            raise HTTPException(status_code=400, detail="SKU cannot be empty")

        dupe_query = {
            "Client": client_id,
            "Identifiers.SKU": new_sku,
            "_id": {"$ne": doc_id},
        }
        duplicate = customsku_collection.find_one(dupe_query, {"_id": 1})
        if duplicate:
            raise HTTPException(status_code=409, detail="SKU already exists for this client")

        set_ops["Identifiers.SKU"] = new_sku

    if data.Category is not None:
        set_ops["Category"] = data.Category

    if data.Global_Promotion is not None:
        set_ops["Global_Promotion"] = data.Global_Promotion

    if data.Locale:
        locale_exists = any(
            isinstance(entry, dict) and entry.get("locale") == data.Locale
            for entry in (existing.get("Locale_Specific_Data") or [])
        )
        if not locale_exists:
            raise HTTPException(status_code=404, detail=f"Locale {data.Locale} not found on CustomSKU")

    if data.Locale_Details:
        locale_set_ops = {}
        locale_unset_ops = {}

        # A field that is explicitly present in the request but null is treated
        # as a clear ($unset). A field that is simply omitted is left untouched.
        # `model_fields_set` lets us tell those two cases apart (pydantic v2).
        provided = data.Locale_Details.model_fields_set
        field_paths = {
            "Title": "Locale_Specific_Data.$[loc].Title",
            "Price": "Locale_Specific_Data.$[loc].MSRP",
            "GTL": "Locale_Specific_Data.$[loc].Guarantees.Labour",
            "GTP": "Locale_Specific_Data.$[loc].Guarantees.Parts",
            "Promo_Code": "Locale_Specific_Data.$[loc].Guarantees.Promotion",
        }
        for field, path in field_paths.items():
            if field not in provided:
                continue
            value = getattr(data.Locale_Details, field)
            if value is None:
                locale_unset_ops[path] = ""
            else:
                locale_set_ops[path] = value

        if locale_set_ops or locale_unset_ops:
            set_ops.update(locale_set_ops)
            unset_ops.update(locale_unset_ops)
            update_kwargs["array_filters"] = [{"loc.locale": data.Locale}]

    if not set_ops and not unset_ops:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    update_doc = {}
    if set_ops:
        update_doc["$set"] = set_ops
    if unset_ops:
        update_doc["$unset"] = unset_ops

    customsku_collection.update_one(
        {"_id": doc_id, "Client": client_id},
        update_doc,
        **update_kwargs
    )

    updated = customsku_collection.find_one({"_id": doc_id, "Client": client_id})
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to load updated CustomSKU")

    # Refresh the embeddable-widget quote cache for any affected locale(s), since
    # MSRP / guarantees / category changes alter pricing.
    from routers.widget_quote import warm_widget_cache
    if data.Locale:
        affected_locales = [data.Locale]
    else:
        affected_locales = [
            entry.get("locale")
            for entry in (updated.get("Locale_Specific_Data") or [])
            if isinstance(entry, dict) and entry.get("locale")
        ]
    for loc in affected_locales:
        background_tasks.add_task(warm_widget_cache, data.ClientKey, str(doc_id), loc)

    return {
        "message": "CustomSKU updated",
        "customsku": _to_id_str(updated),
    }
