from fastapi import APIRouter
from utils.api_docs import json_response
from utils.locale import get_locale_mapping

router = APIRouter(prefix="", tags=["Localization"])

@router.get(
    "/locales",
    summary="List supported locales",
    response_description="Mapping of locale short keys to metadata",
    responses={
        200: json_response(
            "The full mapping. Keys are short codes; values carry the three spellings of the "
            "same locale.",
            {
                "en": {"fastapi": "en_GB", "cms": "en-GB", "label": "English"},
                "fr": {"fastapi": "fr_FR", "cms": "fr-FR", "label": "Français"},
                "nl_BE": {"fastapi": "nl_BE", "cms": "nl-BE", "label": "Nederlands (België)"},
            },
        )
    },
)
async def list_locales():
    """
    Return the static locale mapping shared by the backend, the CMS and the frontend.

    Each entry translates one short key into the three spellings the platform uses:

    | Field | Meaning |
    | --- | --- |
    | `fastapi` | The underscore form this API expects wherever a `locale` is required (`en_GB`). |
    | `cms` | The hyphenated form Strapi uses (`en-GB`). |
    | `label` | The name to show a user, in their own language. |

    Takes no parameters and needs no authentication. This is a **code constant**, not a database
    lookup — a locale listed here still needs a `Locale_Params` record before devices can be
    registered against it, which `GET /locale/locale-details` will confirm.
    """
    return get_locale_mapping()
