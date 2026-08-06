from fastapi import APIRouter, HTTPException, Query, Depends
import requests
import os
from dotenv import load_dotenv
from utils.api_docs import error
from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/upc",
    tags=["Enrichment"]
)

GO_UPC_API_KEY = os.getenv("GO_UPC_TOKEN")
BASE_URL = "https://go-upc.com/api/v1/code"
REQUEST_TIMEOUT = 5  # seconds

@router.get(
    "/lookup",
    dependencies=[Depends(verify_token)],
    summary="Look up a barcode with Go-UPC",
    response_description="Go-UPC's product record, passed through unchanged.",
    responses={
        200: {
            "description": (
                "Go-UPC recognised the barcode. The body is **Go-UPC's own JSON**, forwarded "
                "verbatim — its shape is defined by Go-UPC, not by this API."
            ),
            "content": {"application/json": {}},
        },
        404: error("Go-UPC does not recognise this barcode.", "GTIN not found in UPC"),
        500: error("`GO_UPC_TOKEN` is not configured on this deployment.", "Go-UPC API key not set in environment"),
        502: error("Go-UPC could not be reached, or timed out (5 second limit).", "Error reaching Go-UPC API: ..."),
    },
)
def lookup_go_upc(
    gtin: str = Query(..., description="**Mandatory.** The barcode to look up.", examples=["5011773057240"])
):
    """
    Look up a barcode with Go-UPC — the secondary enrichment source, used when Icecat has no
    entry for a product.

    `gtin` is mandatory. The response is **Go-UPC's JSON, unmodified**, so treat the body as
    their contract rather than this API's. Coverage is broader than Icecat's but the detail is
    thinner: expect a name, brand and image rather than a full specification sheet.

    Any other error status from Go-UPC is passed through with its original code. Nothing is
    stored.
    """
    if not GO_UPC_API_KEY:
        raise HTTPException(status_code=500, detail="Go-UPC API key not set in environment")

    url = f"{BASE_URL}/{gtin}"
    headers = {
        "Authorization": f"Bearer {GO_UPC_API_KEY}"
    }

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail="GTIN not found in UPC")
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"UPC API error {response.status_code}: {response.text}"
            )

    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Error reaching Go-UPC API: {str(e)}")
