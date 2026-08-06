from fastapi import APIRouter, HTTPException, Query, Depends
import requests
import os
from dotenv import load_dotenv
from utils.api_docs import error
from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/ice",
    tags=["Enrichment"]
)

ICECAT_USERNAME = os.getenv("ICECAT_USER", "")
BASE_URL = "https://live.icecat.biz/api/"

@router.get(
    "/lookup",
    dependencies=[Depends(verify_token)],
    summary="Look up a product sheet in Icecat",
    response_description="Icecat's product sheet, passed through unchanged.",
    responses={
        200: {
            "description": (
                "Icecat returned a product. The body is **Icecat's own JSON**, forwarded "
                "verbatim — its shape is defined by Icecat, not by this API."
            ),
            "content": {"application/json": {}},
        },
        400: error("Neither a `gtin` nor a `brand`+`productcode` pair was supplied.", "You must provide a GTIN or both brand and productcode"),
        404: error("Icecat has no product for any of the criteria tried.", "Product not found in ICECAT using provided criteria"),
    },
)
def lookup_icecat(
    lang: str = Query("en", description="Two-letter language code for the returned product sheet.", examples=["en"]),
    gtin: str = Query(None, description="Barcode / GTIN. Tried first; the most reliable identifier.", examples=["5011773057240"]),
    brand: str = Query(None, description="Brand name. Used only with `productcode`, and only if the GTIN lookup fails.", examples=["Bosch"]),
    productcode: str = Query(None, description="Manufacturer product code. Used only with `brand`.", examples=["SMS6ZCI00G"]),
):
    """
    Fetch a product sheet from Icecat — titles, images, specifications and documents used to
    enrich the SKU catalogue.

    **Supply either a `gtin` or both `brand` and `productcode`**; sending neither is rejected
    with `400`. When both are supplied the GTIN is tried first and the brand pair is the
    fallback, so passing both maximises the chance of a hit.

    The response is **Icecat's JSON, unmodified** — this endpoint is a pass-through, so treat the
    body as Icecat's contract rather than this API's. Nothing is stored; use
    `POST /sku/create_master_sku` to persist enrichment.
    """
    if not gtin and not (brand and productcode):
        raise HTTPException(
            status_code=400,
            detail="You must provide a GTIN or both brand and productcode"
        )

    def build_url(use_gtin=True):
        base = f"{BASE_URL}?username={ICECAT_USERNAME}&lang={lang}"
        if use_gtin:
            return f"{base}&GTIN={gtin}"
        else:
            return f"{base}&brand={brand}&productcode={productcode}"

    # Step 1: Try GTIN
    if gtin:
        gtin_url = build_url(use_gtin=True)
        gtin_response = requests.get(gtin_url)
        if gtin_response.status_code == 200:
            return gtin_response.json()

        # Optional log
        print(f"GTIN lookup failed ({gtin_response.status_code}): {gtin_url}")

    # Step 2: Try brand + productcode if available
    if brand and productcode:
        brand_url = build_url(use_gtin=False)
        brand_response = requests.get(brand_url)
        if brand_response.status_code == 200:
            return brand_response.json()

        # Optional log
        print(f"Brand/ProductCode lookup failed ({brand_response.status_code}): {brand_url}")

    # If both fail
    raise HTTPException(status_code=404, detail="Product not found in ICECAT using provided criteria")
