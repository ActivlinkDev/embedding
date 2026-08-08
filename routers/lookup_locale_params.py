from fastapi import APIRouter, HTTPException, Query
from pymongo import MongoClient
import os
from dotenv import load_dotenv

from utils.api_docs import error, json_response

load_dotenv()

router = APIRouter(
    prefix="/locale",
    tags=["Localization"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["Locale_Params"]  # ✅ your target collection

@router.get(
    "/locale-details",
    summary="Fetch the operating parameters of a locale",
    response_description="The stored Locale_Params document, without its Mongo `_id`.",
    responses={
        200: json_response(
            "The locale is configured. Fields vary by market; `currency` is the one other "
            "endpoints depend on.",
            {
                "locale": "en_GB",
                "country": "United Kingdom",
                "currency": "GBP",
                "language": "en",
                "vatRate": 20,
            },
        ),
        404: error("This locale is not configured in `Locale_Params`.", "No details found for the specified locale"),
    },
)
def get_locale_details(
    locale: str = Query(
        ...,
        description="**Mandatory.** Locale code to look up, e.g. `en_GB`. Matched exactly — case and separator matter.",
        examples=["en_GB"],
    )
):
    """
    Return the operating parameters configured for one locale.

    A locale must exist here before devices can be registered against it: registration reads the
    `currency` from this record rather than trusting the caller, and rejects any locale that is
    missing with `400`.

    **No authentication is required** for this lookup. For the static language/CMS mapping used
    by the frontend, use `GET /locales` instead — that one is a code constant, not a database
    record.
    """
    result = collection.find_one({"locale": locale}, {"_id": 0})
    
    if result:
        return result
    else:
        raise HTTPException(status_code=404, detail="No details found for the specified locale")
