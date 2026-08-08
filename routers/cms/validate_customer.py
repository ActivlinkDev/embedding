from fastapi import APIRouter, HTTPException, Query, Depends
import httpx
import os
from pymongo import MongoClient
from utils.api_docs import error
from utils.dependencies import verify_token
from utils.locale import resolve_strapi_locale, LocaleNotSupportedError

router = APIRouter(tags=["CMS"]) 

# Updated: singular endpoint validate-customer (not plural) per new requirement
STRAPI_BASE_URL = "https://strapi-production-5603.up.railway.app/api/validate-customer"
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")
if not STRAPI_BEARER_TOKEN:
    raise RuntimeError("STRAPI_BEARER_TOKEN environment variable must be set")

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
locale_params_collection = db["Locale_Params"]

@router.get(
    "/cms_validate_customer",
    summary="Fetch the customer-verification page content for a locale",
    response_description="The CMS validate-customer entry for the requested locale.",
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
async def cms_validate_customer(
    locale: str = Query(
        ...,
        description="**Mandatory.** Locale in underscore form (`en_GB`). Mapped to the CMS's hyphenated form automatically.",
        examples=["en_GB"],
    ),
    _: None = Depends(verify_token)
):
    """
    Fetch the localized content for the customer-verification screens — the copy shown while a
    customer confirms their identity by phone and one-time code.

    **Content only.** This endpoint validates nothing and reads no customer data; the actual
    checks are `POST /customer/authenticate` and `POST /otp/verify`.

    `locale` is mandatory, in the API's underscore form, and is mapped to the CMS locale via
    `Locale_Params`. Relations are fully populated. The response is **Strapi's JSON, forwarded
    unchanged**.
    """
    locale_doc = locale_params_collection.find_one({"locale": locale})
    try:
        _, strapi_locale = resolve_strapi_locale(locale, locale_doc)
    except LocaleNotSupportedError as e:
        raise HTTPException(status_code=400, detail=str(e))

    params = {"locale": strapi_locale, "populate": "*"}
    headers = {"Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"}

    try:
        async with httpx.AsyncClient() as client_http:
            response = await client_http.get(STRAPI_BASE_URL, params=params, headers=headers, timeout=15.0)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f"Strapi error: {response.text}")
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
