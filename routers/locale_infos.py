from fastapi import APIRouter

router = APIRouter(
    prefix="/locale_infos",
    tags=["Localization"]
)


@router.get("/", summary="List locale infos")
def list_locale_infos():
    """
    Minimal shim for `routers.locale_infos` so the application can start.
    Returns an empty list by default; extend to proxy Strapi or other store.
    """
    return {"locale_infos": []}
