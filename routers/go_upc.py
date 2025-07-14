from fastapi import APIRouter, HTTPException, Query, Depends
import requests
import os
from dotenv import load_dotenv
from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/upc",
    tags=["UPC Lookup"]
)

GO_UPC_API_KEY = os.getenv("GO_UPC_TOKEN")
BASE_URL = "https://go-upc.com/api/v1/code"

@router.get("/lookup", dependencies=[Depends(verify_token)])
def lookup_go_upc(
    gtin: str = Query(..., description="The GTIN to look up")
):
    url = f"{BASE_URL}/{gtin}?key={GO_UPC_API_KEY}"
    response = requests.get(url)

    if response.status_code == 200:
        return response.json()
    elif response.status_code == 404:
        raise HTTPException(status_code=404, detail="GTIN not found in UPC")
    else:
        raise HTTPException(
            status_code=500,
            detail=f"UPC API error {response.status_code}: {response.text}"
        )
