from fastapi import APIRouter, HTTPException, Query, Depends
import requests
import os
from dotenv import load_dotenv
from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/upc",
    tags=["Enrich"]
)

GO_UPC_API_KEY = os.getenv("GO_UPC_TOKEN")
BASE_URL = "https://go-upc.com/api/v1/code"
REQUEST_TIMEOUT = 5  # seconds

@router.get("/lookup", dependencies=[Depends(verify_token)])
def lookup_go_upc(
    gtin: str = Query(..., description="The GTIN to look up")
):
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
