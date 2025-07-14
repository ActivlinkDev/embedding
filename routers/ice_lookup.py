from fastapi import APIRouter, HTTPException, Query, Depends
import requests
import os
from dotenv import load_dotenv
from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/ice",
    tags=["ICE Lookup"]
)

ICECAT_USERNAME = os.getenv("ICECAT_USER", "")
BASE_URL = "https://live.icecat.biz/api/"

@router.get("/lookup", dependencies=[Depends(verify_token)])
def lookup_icecat(
    lang: str = Query("en", description="2-letter language code (e.g. en, fr, es)"),
    gtin: str = Query(None, description="GTIN for ICE lookup"),
    brand: str = Query(None, description="Brand name (used if GTIN fails)"),
    productcode: str = Query(None, description="Product code (used if GTIN fails)")
):
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
