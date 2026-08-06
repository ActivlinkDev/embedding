from fastapi import APIRouter, Depends
from utils.api_docs import json_response, secured
from utils.common import device_categories
from utils.dependencies import verify_token

router = APIRouter(
    prefix="/categories",
    tags=["Catalog"]
)

@router.get(
    "/",
    summary="List the in-process device categories",
    response_description="The category names currently loaded in the process.",
    responses=secured({
        200: json_response("The loaded category list.", {"categories": ["Dishwasher", "Washing Machine", "Television"]}),
    }),
)
def list_categories(_: None = Depends(verify_token)):
    """
    Return the device categories held in memory by this process
    (`utils.common.device_categories`).

    Takes no parameters. Note that this list is populated at runtime and is **empty by default**
    — the precomputed category embeddings this module once loaded from disk were removed, so a
    deployment that does not populate it will get `{"categories": []}`.

    For category matching driven by the catalogue rather than this list, use `POST /match`,
    which runs a vector search against the `Category` collection.
    """
    return {"categories": list(device_categories)}
