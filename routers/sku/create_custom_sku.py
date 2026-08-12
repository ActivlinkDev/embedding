# create_custom_sku.py

from fastapi import APIRouter, HTTPException, Depends, Request, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timezone
import os
import re

from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from .create_master_sku import create_master_sku, MasterSKURequest, _run_dseo_task

# NOTE: This endpoint runs as a synchronous `def` so FastAPI executes it in a
# threadpool. That keeps its blocking work (pymongo + the inline MasterSKU
# creation, which itself does blocking HTTP/DB calls) off the event loop.
# Background SERP enrichment is scheduled inside create_master_sku via
# BackgroundTasks, so this module no longer schedules anything itself.

# ==== HELPERS ====

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def validate_mandatory_fields(data) -> List[str]:
    missing_fields = []
    if not data.ClientKey or not data.ClientKey.strip():
        missing_fields.append("ClientKey")
    if not data.Locale or not data.Locale.strip():
        missing_fields.append("Locale")
    if not data.SKU or not data.SKU.strip():
        missing_fields.append("SKU")
    if not data.Source or not data.Source.strip():
        missing_fields.append("Source")
    if (not data.GTIN or not data.GTIN.strip()) and (not data.Make or not data.Make.strip() or not data.Model or not data.Model.strip()):
        missing_fields.append("GTIN or (Make and Model)")
    return missing_fields

def locale_exists(locale_data_list, locale: str) -> bool:
    return any(entry.get("locale") == locale for entry in locale_data_list)

def find_locale_data(locale_specific_data, locale: str):
    return next((entry for entry in locale_specific_data if entry.get("locale") == locale), {})

def build_identifiers(mastersku, sku: str) -> dict:
    return {
        "GTIN": mastersku.get("GTIN", []),
        "Make": mastersku.get("Make", ""),
        "Model": mastersku.get("Model", ""),
        "SKU": sku
    }

def master_query_for(data) -> Optional[dict]:
    """Build the MasterSKU lookup query from GTIN (preferred) or Make+Model.

    Make/Model are escaped so product codes containing regex metacharacters can't
    break the query or match unintended documents.
    """
    if data.GTIN and data.GTIN.strip():
        return {"GTIN": {"$in": [data.GTIN]}}
    if data.Make and data.Model:
        return {
            "Make": {"$regex": f"^{re.escape(data.Make)}$", "$options": "i"},
            "Model": {"$regex": re.escape(data.Model), "$options": "i"},
        }
    return None

def build_locale_data(
    data, locale_details, locale_info, client_info, mastersku_locale=None
) -> dict:
    def fallback(val, fallback_val):
        if val is not None and val != "" and (not isinstance(val, (int, float)) or val != 0):
            return val
        return fallback_val

    d = {
        "locale": data.Locale,
        "Title": fallback(
            locale_details.Title, mastersku_locale.get("Input_Title") if mastersku_locale else ""
        ),
        "Generate_Offers": "Y",
        "MSRP": fallback(
            locale_details.Price, mastersku_locale.get("Price") if mastersku_locale else 0
        ),
        "Currency": fallback(
            locale_info.get("currency", ""),
            mastersku_locale.get("Currency") if mastersku_locale else "",
        ),
        "created_at": utc_now_iso(),
        "Guarantees": {
            "Labour": (
                locale_details.GTL if locale_details.GTL not in (None, "", 0)
                else locale_info.get("gtee_labour", 0)
            ),
            "Parts": (
                locale_details.GTP if locale_details.GTP not in (None, "", 0)
                else locale_info.get("gtee_parts", 0)
            ),
            "Promotion": locale_details.Promo_Code or "",
        },
        "Custom_Links": (
            [cl.dict() for cl in (locale_details.Custom_Links or [])]
            if getattr(locale_details, "Custom_Links", None)
            else [
                {"Type": "QR", "URL": ""},
                {"Type": "Service", "URL": ""},
                {"Type": "Recycle", "URL": ""},
            ]
        ),
    }
    # Include the localized matched category string from the MasterSKU locale block, if available
    try:
        if mastersku_locale and isinstance(mastersku_locale, dict):
            lm = mastersku_locale.get("Locale_Matched_Category")
            d["Locale_Matched_Category"] = lm if lm not in (None, "") else None
        else:
            d["Locale_Matched_Category"] = None
    except Exception:
        # Don't let localization lookup break creation
        d["Locale_Matched_Category"] = None
    return d

def build_existing_query(client_name, data):
    """Find a client's existing CustomSKU for this product.

    Matching is by client plus identifiers only. Source is deliberately not part
    of the key: a client's catalog holds one document per product regardless of
    which integration created it, and a new Source is recorded on the existing
    document instead of producing a duplicate.
    """
    sku_cond = {
        "Client": client_name,
        "Identifiers.SKU": data.SKU,
    }
    gtin_cond = (
        {
            "Client": client_name,
            "Identifiers.GTIN": {"$in": [data.GTIN]},
        }
        if data.GTIN and data.GTIN.strip() else None
    )
    make_model_cond = (
        {
            "Client": client_name,
            "Identifiers.Make": {"$regex": f"^{re.escape(data.Make)}$", "$options": "i"},
            "Identifiers.Model": {"$regex": f"^{re.escape(data.Model)}$", "$options": "i"},
        }
        if data.Make and data.Model else None
    )
    or_conditions = [sku_cond]
    if gtin_cond: or_conditions.append(gtin_cond)
    if make_model_cond: or_conditions.append(make_model_cond)
    return {"$or": or_conditions}

# ==== END HELPERS ====

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Catalog"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]

locale_collection = db["Locale_Params"]
client_collection = db["ClientKey"]
customsku_collection = db["CustomSKU"]
mastersku_collection = db["MasterSKU"]

class CustomLink(BaseModel):
    """An extra link shown alongside the product. Both fields are mandatory."""

    Type: str = Field(..., description="**Mandatory.** What the link is, e.g. `manual`, `spec_sheet`.", examples=["manual"])
    URL: str = Field(..., description="**Mandatory.** The link target.", examples=["https://example.com/manuals/sms6zci00g.pdf"])

class LocaleDetails(BaseModel):
    """Client overrides for one locale. Every field is optional — anything left blank or `0`
    falls back to the MasterSKU's value for that locale."""

    Title: Optional[str] = Field("", description="Display title. Falls back to the MasterSKU title.", examples=["Bosch Series 6 Dishwasher"])
    Price: Optional[float] = Field(0, description="The client's selling price (MSRP). Falls back to the MasterSKU price.", examples=[449.99])
    GTL: Optional[int] = Field(0, description="Guarantee, labour, in months.", examples=[12])
    GTP: Optional[int] = Field(0, description="Guarantee, parts, in months.", examples=[24])
    Promo_Code: Optional[str] = Field("", description="Promotion applied for this locale.", examples=["SUMMER25"])
    Custom_Links: Optional[List[CustomLink]] = Field(None, description="Extra product links to surface with this SKU.")

class CustomSKURequest(BaseModel):
    """A client catalogue entry to create, or a locale to add to an existing one.

    Mandatory: `ClientKey`, `Locale`, `SKU`, `Source`, **plus** either `GTIN` or both `Make` and
    `Model` — without one of those the MasterSKU cannot be matched and nothing is created.
    """

    ClientKey: str = Field(..., description="**Mandatory.** Tenant key the SKU belongs to.", examples=["acme_uk_live"])
    Locale: str = Field(
        ...,
        description="**Mandatory.** Locale being added. Must exist in `Locale_Params`, otherwise `404`.",
        examples=["en_GB"],
    )
    SKU: str = Field(..., description="**Mandatory.** The client's own SKU code.", examples=["BOSCH-DW-4421"])
    Source: str = Field(..., description="**Mandatory.** Channel this SKU is sold through, e.g. `web`.", examples=["web"])
    GTIN: Optional[str] = Field(
        "",
        description="**Mandatory unless `Make` and `Model` are both given.** Barcode used to find or create the MasterSKU.",
        examples=["5011773057240"],
    )
    Make: Optional[str] = Field("", description="Manufacturer. Required with `Model` when no `GTIN` is supplied.", examples=["Bosch"])
    Model: Optional[str] = Field("", description="Model designation. Required with `Make` when no `GTIN` is supplied.", examples=["SMS6ZCI00G"])
    Category: Optional[str] = Field("", description="Category override. Falls back to the MasterSKU's category.", examples=["Dishwasher"])
    Locale_Details: Optional[LocaleDetails] = Field(None, description="Per-locale overrides. Omit to inherit everything from the MasterSKU.")
    Global_Promotion: Optional[str] = Field(None, description="Promotion applied across every locale of this SKU.", examples=["LAUNCH10"])
    add_pricing: Optional[bool] = Field(True, description="Whether cover pricing should be attached to this SKU.", examples=[True])

    model_config = {
        "json_schema_extra": {
            "example": {
                "ClientKey": "acme_uk_live",
                "Locale": "en_GB",
                "SKU": "BOSCH-DW-4421",
                "Source": "web",
                "GTIN": "5011773057240",
                "Make": "Bosch",
                "Model": "SMS6ZCI00G",
                "Category": "Dishwasher",
                "Locale_Details": {
                    "Title": "Bosch Series 6 Dishwasher",
                    "Price": 449.99,
                    "GTL": 12,
                    "GTP": 24,
                    "Promo_Code": "",
                    "Custom_Links": [{"Type": "manual", "URL": "https://example.com/manuals/sms6zci00g.pdf"}],
                },
                "Global_Promotion": None,
                "add_pricing": True,
            }
        }
    }


def ensure_master_with_locale(data, request, background_tasks):
    """Return the MasterSKU document that contains data.Locale.

    If a matching MasterSKU already has the locale, it's returned as-is.
    Otherwise create_master_sku is invoked synchronously to create the
    MasterSKU (or add the locale). Because that call commits its write before
    returning, a single re-read is enough — no polling/sleep loop required.

    Returns None only when there are no identifiers (GTIN or Make+Model) to
    match on.
    """
    query = master_query_for(data)
    if not query:
        return None

    master = mastersku_collection.find_one(query)
    if master and locale_exists(master.get("Locale_Specific_Data", []), data.Locale):
        # MasterSKU already has this locale — still fire DSEO pricing if requested
        if data.add_pricing:
            background_tasks.add_task(_run_dseo_task, data.Locale, str(master["_id"]))
        return master

    master_data = MasterSKURequest(
        Make=data.Make or "",
        Model=data.Model or "",
        GTIN=data.GTIN or "",
        locale=data.Locale,
        Category=data.Category,
    )
    # Synchronous call — create_master_sku persists before returning and
    # schedules its own background DataforSEO enrichment via BackgroundTasks.
    create_master_sku(
        master_data,
        request=request,
        background_tasks=background_tasks,
        add_pricing=data.add_pricing,
    )
    return mastersku_collection.find_one(query)


def _serialize(doc):
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


def _synthetic_request() -> Request:
    """Minimal Request for callers that have no HTTP request of their own.

    Only ever read for its base_url, and only when FASTAPI_BASE_URL /
    PUBLIC_BACKEND_URL are unset, so a placeholder host is sufficient.
    """
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        # Matches the codebase's localhost fallback, so masked URLs built from
        # this request look the same as everywhere else when no base URL is set.
        "scheme": "http",
        "server": ("localhost", 8000),
        "query_string": b"",
        "root_path": "",
    })


def create_custom_sku_service(
    data: CustomSKURequest,
    background_tasks: BackgroundTasks,
    request: Optional[Request] = None,
) -> dict:
    """Create a CustomSKU, or add a locale to one that already exists.

    The implementation behind POST /sku/create_custom_sku, callable directly by
    importers and reprocessors that have no incoming HTTP request. Raises the
    same HTTPExceptions as the endpoint, so callers can let them propagate.
    """
    if request is None:
        request = _synthetic_request()
    # 0. Validate inputs
    missing_fields = validate_mandatory_fields(data)
    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing mandatory input(s): {', '.join(missing_fields)}"
        )

    # 1. Lookup locale
    locale_info = locale_collection.find_one({"locale": data.Locale})
    if not locale_info:
        raise HTTPException(status_code=404, detail=f"Locale {data.Locale} not found.")

    # 2. Lookup client
    client_info = client_collection.find_one({"ClientKey": data.ClientKey})
    if not client_info:
        raise HTTPException(status_code=404, detail=f"ClientKey {data.ClientKey} not found.")
    client_name = client_info.get("Client_ID", "")

    # 3. Check for an existing CustomSKU (by SKU, GTIN, or Make+Model)
    existing = customsku_collection.find_one(build_existing_query(client_name, data))

    if existing:
        # Record this integration as a source of the product. The document is
        # shared across sources, so provenance accumulates rather than forking.
        customsku_collection.update_one(
            {"_id": existing["_id"]},
            {"$addToSet": {"Sources": data.Source}},
        )

        # 3a. Already has this locale — nothing to do.
        if locale_exists(existing.get("Locale_Specific_Data", []), data.Locale):
            return {
                "message": "SKU exists already for client and locale",
                "existing": _serialize(customsku_collection.find_one({"_id": existing["_id"]})),
            }

        # 3b. Ensure the MasterSKU carries this locale, then append it to the CustomSKU.
        master = ensure_master_with_locale(data, request, background_tasks)
        master_locale = find_locale_data(master.get("Locale_Specific_Data", []), data.Locale) if master else {}
        if not master_locale:
            return {"message": "Master SKU creation is taking longer than expected. Please try again in a few seconds."}

        locale_details = data.Locale_Details or LocaleDetails()
        locale_data = build_locale_data(data, locale_details, locale_info, client_info, mastersku_locale=master_locale)
        customsku_collection.update_one(
            {"_id": existing["_id"]},
            {"$push": {"Locale_Specific_Data": locale_data}},
        )
        # Warm the embeddable-widget quote cache for the newly added locale.
        from routers.widget_quote import warm_widget_cache
        background_tasks.add_task(warm_widget_cache, data.ClientKey, str(existing["_id"]), data.Locale)
        persisted = customsku_collection.find_one({"_id": existing["_id"]})
        return {"message": "Locale added to existing CustomSKU", "customsku": _serialize(persisted)}

    # 4. No existing CustomSKU — ensure the MasterSKU exists, then create the CustomSKU.
    master = ensure_master_with_locale(data, request, background_tasks)
    if master is None:
        return {"message": "No GTIN or Make/Model supplied for MasterSKU matching, unable to proceed."}

    master_locale = find_locale_data(master.get("Locale_Specific_Data", []), data.Locale)
    if not master_locale:
        return {"message": "Master SKU creation is taking longer than expected. Please try again in a few seconds."}

    locale_details = data.Locale_Details or LocaleDetails()
    locale_data = build_locale_data(data, locale_details, locale_info, client_info, mastersku_locale=master_locale)
    category_root = data.Category if data.Category not in (None, "") else master.get("Category", "")

    doc = {
        "Client": client_name,
        "Client_Key": data.ClientKey,
        "Sources": [data.Source],
        "Identifiers": build_identifiers(master, data.SKU),
        "MasterSKU": str(master["_id"]),
        "Category": category_root,
        "Global_Promotion": data.Global_Promotion if data.Global_Promotion is not None else None,
        "Locale_Specific_Data": [locale_data],
    }
    result = customsku_collection.insert_one(doc)
    # Warm the embeddable-widget quote cache so the first shopper is fast too.
    from routers.widget_quote import warm_widget_cache
    background_tasks.add_task(warm_widget_cache, data.ClientKey, str(result.inserted_id), data.Locale)
    persisted = customsku_collection.find_one({"_id": result.inserted_id})
    return _serialize(persisted)


@router.post(
    "/create_custom_sku",
    summary="Create a client SKU, or add a locale to an existing one",
    response_description="The stored CustomSKU, or a message explaining what happened instead.",
    responses=secured({
        200: json_response(
            "Processed. **Check the response shape** — a new SKU returns the document itself, "
            "while every other outcome returns a `message` (and sometimes the existing record).",
            {
                "_id": "681aa2f1c4b21d0f8c9e0012",
                "Client": "ACME-UK",
                "Client_Key": "acme_uk_live",
                "Sources": ["web"],
                "Identifiers": {"GTIN": ["5011773057240"], "Make": "Bosch", "Model": "SMS6ZCI00G", "SKU": "BOSCH-DW-4421"},
                "MasterSKU": "681aa2f1c4b21d0f8c9e0044",
                "Category": "Dishwasher",
                "Global_Promotion": None,
                "Locale_Specific_Data": [
                    {"locale": "en_GB", "Title": "Bosch Series 6 Dishwasher", "MSRP": 449.99, "Guarantees": {"Parts": "24", "Labour": "12"}}
                ],
            },
        ),
        400: error(
            "A mandatory field is blank, or neither `GTIN` nor `Make`+`Model` was supplied.",
            "Missing mandatory input(s): GTIN or (Make and Model)",
        ),
        404: error("The `Locale` is not configured, or the `ClientKey` is unknown.", "Locale en_GB not found."),
    }),
)
def create_custom_sku(
    data: CustomSKURequest,
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_token),
):
    """
    Create a CustomSKU for a client, or add a locale to one that already exists.

    A CustomSKU always hangs off a MasterSKU, so the endpoint resolves that first: it looks for a
    MasterSKU by `GTIN`, else by `Make`+`Model`, and **creates one if none exists** (which may
    trigger background enrichment). That is why one of those identifiers is mandatory.

    **Four outcomes, all `200` — branch on the response, not the status code:**

    | Situation | Response |
    | --- | --- |
    | New SKU created | The CustomSKU document itself, with `_id` |
    | SKU exists, new locale added | `{"message": "Locale added to existing CustomSKU", "customsku": {…}}` |
    | SKU already covers this locale | `{"message": "SKU exists already for client and locale", "existing": {…}}` |
    | MasterSKU still being built | `{"message": "Master SKU creation is taking longer than expected. Please try again in a few seconds."}` — retry shortly |

    Anything omitted from `Locale_Details` is inherited from the MasterSKU's data for that locale,
    so a minimal call still produces a priced, titled SKU. After a successful write the widget
    quote cache is warmed in the background, so the first shopper does not pay for a cold cache.
    """
    return create_custom_sku_service(data, background_tasks, request=request)
