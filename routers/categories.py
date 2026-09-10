from typing import Optional

from fastapi import APIRouter, Depends, Query

from utils.api_docs import json_response, secured
from utils.category_tree import all_categories
from utils.dependencies import verify_token

router = APIRouter(
    prefix="/categories",
    tags=["Catalog"]
)

@router.get(
    "/",
    summary="List the device categories in the taxonomy",
    response_description="Every category in the `Category` collection, with its locale title.",
    responses=secured({
        200: json_response("The taxonomy's categories, ordered by title.", {"categories": [
            {"category": "Dishwasher", "title": "Dishwasher", "group": "Kitchen", "sector": "Home Appliances"},
            {"category": "Washer Dryer", "title": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances"},
        ]}),
    }),
)
def list_categories(
    locale: Optional[str] = Query(
        None,
        description="Locale for `title`, e.g. `en_GB`. Falls back like the product card's "
                    "category display: same language first, then en_GB, then the taxonomy spelling.",
        examples=["en_GB"],
    ),
    _: None = Depends(verify_token),
):
    """
    Return every category in the `Category` taxonomy, ordered by its locale-facing title.

    Meant for populating a category picker where a device cannot be matched against the
    catalogue (no GTIN, no vision match) and the category has to be supplied explicitly instead
    of derived — see `POST /device-register`'s `allow_manual` / `Identifiers.category` and
    `POST /sku/create_custom_sku`'s `Category` field, both of which validate against this same
    taxonomy.

    `category` is the taxonomy spelling that rating and assignment match on; `title` is the name
    to show the customer for `locale`. Reads the live `Category` collection (cached in-process,
    see `utils.category_tree`), so an unreadable taxonomy returns `{"categories": []}` rather
    than an error.
    """
    return {"categories": all_categories(locale)}
