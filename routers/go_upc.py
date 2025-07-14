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

GO_UPC_API_URL = "https://go-upc.com/api/v1/code"
GO_UPC_TOKEN = os.getenv("GO_UPC_TOKEN")

@router.get("/lookup", dependencies=[Depends(verify_token)])
def lookup_go_upc(
    gtin: str = Query(..., description="The GTIN to look up")
):
    url = f"{GO_UPC_API_URL}/{gtin}"
    headers = {
        "Authorization": f"Bearer {GO_UPC_TOKEN}"
    }

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        return response.json()
    elif response.status_code == 404:
        raise HTTPException(status_code=404, detail="GTIN not found in UPC")
    else:
        raise HTTPException(
            status_code=500,
            detail=f"UPC API error {response.status_code}: {response.text}"
        )
