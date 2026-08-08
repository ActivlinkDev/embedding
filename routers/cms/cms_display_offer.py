from fastapi import APIRouter, HTTPException, Query, Depends
import httpx
import os
from pymongo import MongoClient
from utils.api_docs import error
from utils.dependencies import verify_token
from utils.locale import resolve_strapi_locale, LocaleNotSupportedError

router = APIRouter(tags=["CMS"]) 

STRAPI_BASE_URL = "https://strapi-production-5603.up.railway.app/api/display-offer"
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")
if not STRAPI_BEARER_TOKEN:
    raise RuntimeError("STRAPI_BEARER_TOKEN environment variable must be set")

# Lazily create Mongo client at request time to avoid DNS/SRV lookups during module import
_mongo_client = None

def _get_locale_params_collection():
    global _mongo_client
    try:
        if _mongo_client is None:
            _mongo_client = MongoClient(os.getenv("MONGO_URI"), connect=False)
        db = _mongo_client["Activlink"]
        return db["Locale_Params"]
    except Exception:
        return None

@router.get(
    "/cms_display_offer",
    summary="Fetch the offer-page content for a locale",
    response_description="The CMS display-offer entry for the requested locale.",
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
async def cms_display_offer(
    locale: str = Query(
        ...,
        description="**Mandatory.** Locale in underscore form (`en_GB`). Mapped to the CMS's hyphenated form automatically.",
        examples=["en_GB"],
    ),
    _: None = Depends(verify_token)
):
    """
    Fetch the localized content for the cover offer page — headings, benefit copy and calls to
    action shown when cover is presented to a customer.

    `locale` is mandatory and is given in the API's underscore form; the CMS mapping comes from
    `Locale_Params` where available, otherwise from a built-in table. A locale with no CMS
    equivalent returns `400`.

    The response is **Strapi's JSON, forwarded unchanged**. Product-specific copy comes from
    `GET /props_lookup` instead.
    """
    locale_doc = None
    try:
        col = _get_locale_params_collection()
        if col is not None:
            locale_doc = col.find_one({"locale": locale})
    except Exception:
        locale_doc = None
    try:
        _, strapi_locale = resolve_strapi_locale(locale, locale_doc)
    except LocaleNotSupportedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    params = {"locale": strapi_locale}
    headers = {"Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"}

    try:
        async with httpx.AsyncClient() as client_http:
            response = await client_http.get(STRAPI_BASE_URL, params=params, headers=headers, timeout=15.0)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f"Strapi error: {response.text}")
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
