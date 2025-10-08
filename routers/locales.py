from fastapi import APIRouter
from utils.locale import get_locale_mapping

router = APIRouter(prefix="", tags=["Locales"])

@router.get("/locales", summary="List supported locales", response_description="Mapping of locale short keys to metadata")
async def list_locales():
    """Return the static locale mapping used by backend & frontend.

    Shape:
    {
      "en": {"fastapi": "en_GB", "cms": "en-GB", "label": "English"},
      ...
    }
    """
    return get_locale_mapping()
