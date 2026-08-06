from fastapi import APIRouter

from utils.api_docs import json_response

router = APIRouter(
    prefix="/locale_infos",
    tags=["Localization"]
)


@router.get(
    "/",
    summary="List locale infos",
    response_description="Locale info records, keyed under `locale_infos`.",
    responses={200: json_response("The (currently always empty) list of locale infos.", {"locale_infos": []})},
)
def list_locale_infos():
    """
    Return locale info records.

    **This endpoint is a placeholder.** It exists so the application can start with the router
    map intact, and it always responds `{"locale_infos": []}` — it is not yet wired to Strapi or
    any other store. Do not build against it expecting data.

    Takes no parameters and needs no authentication. For real locale data use `GET /locales`
    (static mapping) or `GET /locale/locale-details` (per-locale configuration).
    """
    return {"locale_infos": []}
