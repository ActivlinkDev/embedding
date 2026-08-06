from fastapi import APIRouter, HTTPException, Query, Depends
from typing import List
import httpx
import os
from pymongo import MongoClient
from utils.api_docs import error
from utils.dependencies import verify_token
from utils.locale import resolve_strapi_locale, LocaleNotSupportedError

router = APIRouter(tags=["CMS"]) 

STRAPI_BASE_URL = "https://strapi-production-5603.up.railway.app/api/props"
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")
if not STRAPI_BEARER_TOKEN:
    raise RuntimeError("STRAPI_BEARER_TOKEN environment variable must be set")

# Lazily create Mongo client at request time to avoid DNS/SRV lookups during module import
_mongo_client = None

def _get_locale_params_collection():
    global _mongo_client
    try:
        if _mongo_client is None:
            # connect=False defers initial connection; for mongodb+srv this may still resolve at first use
            _mongo_client = MongoClient(os.getenv("MONGO_URI"), connect=False)
        db = _mongo_client["Activlink"]
        return db["Locale_Params"]
    except Exception:
        # If Mongo is unreachable or DNS fails, fall back to pure transform without DB mapping
        return None

@router.get(
    "/props_lookup",
    summary="Fetch product marketing copy from the CMS",
    response_description="The CMS entries for the requested products, in the requested locale.",
    responses={
        200: {
            "description": (
                "Strapi's response, forwarded unchanged. The shape is defined by the CMS "
                "content type, not by this API."
            ),
            "content": {"application/json": {}},
        },
        400: error("The locale has no Strapi equivalent configured.", "Locale 'xx_XX' is not supported"),
        500: error("Strapi could not be reached.", "..."),
    },
)
async def props_lookup(
    locale: str = Query(
        ...,
        description="**Mandatory.** Locale in underscore form (`es_ES`). Translated to the CMS's hyphenated form (`es-ES`) automatically.",
        examples=["es_ES"],
    ),
    product_ids: List[str] = Query(
        ...,
        alias="product_ids[]",
        description=(
            "**Mandatory.** One or more product ids. Note the parameter name is literally "
            "`product_ids[]`; repeat it once per id."
        ),
        examples=[["EX1", "WF1"]],
    ),
    _: None = Depends(verify_token)
):
    """
    Fetch the marketing copy (props) for one or more cover products from the CMS.

    This is what the storefront and widget render alongside a price: benefit bullets, terms
    summaries and display text, in the customer's language.

    Both parameters are mandatory, and the query parameter really is spelled **`product_ids[]`**
    — repeat it for each id. The `locale` you send is the API's underscore form; it is mapped to
    the CMS's hyphenated form, preferring the mapping stored in `Locale_Params` and falling back
    to a built-in table. A locale with no CMS equivalent returns `400`.

    The response is **Strapi's JSON, forwarded unchanged** — its shape belongs to the CMS content
    type, not to this API.
    """
    # Delegate to the in-process helper so other modules can call this
    # without going through FastAPI dependency injection.
    try:
        result = await fetch_props(locale, product_ids)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def fetch_props(locale: str, product_ids: List[str]):
    """In-process helper that performs the Strapi props lookup and returns parsed JSON.

    This mirrors the endpoint behavior but is callable directly by other routers.
    """
    locale_doc = None
    try:
        col = _get_locale_params_collection()
        if col is not None:
            locale_doc = col.find_one({"locale": locale})
    except Exception:
        # Ignore DB failures and proceed with fallback mapping
        locale_doc = None
    try:
        _, strapi_locale = resolve_strapi_locale(locale, locale_doc)
    except LocaleNotSupportedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    params = [("locale", strapi_locale), ("populate", "*")]
    for idx, pid in enumerate(product_ids):
        params.append((f"filters[Product_ID][$in][{idx}]", pid))

    headers = {"Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"}

    async with httpx.AsyncClient() as client_http:
        response = await client_http.get(STRAPI_BASE_URL, params=params, headers=headers, timeout=15.0)
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=f"Strapi error: {response.text}")
    return response.json()
