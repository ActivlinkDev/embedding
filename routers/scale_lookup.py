from fastapi import APIRouter, HTTPException, Query, Depends
import requests
import os
from dotenv import load_dotenv
from pymongo import MongoClient
from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/scale",
    tags=["SERP Lookup"]
)

mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["Activlink"]
locale_collection = db["Locale_Params"]

SCALE_SERP_API_KEY = os.getenv("SCALE_SERP_KEY")
BASE_URL = "https://api.scaleserp.com/search"
REQUEST_TIMEOUT = 5


@router.get("/shopping", dependencies=[Depends(verify_token)])
def lookup_shopping_trimmed(
    query: str = Query(..., description="Search query (e.g. brand + model)"),
    locale: str = Query(..., description="Locale (e.g. en_GB)")
):
    if not SCALE_SERP_API_KEY:
        raise HTTPException(status_code=500, detail="SERP API key is not set")

    # 🔍 Lookup locale info from DB
    locale_data = locale_collection.find_one(
        {"locale": locale}, {"_id": 0, "google_domain": 1, "hl": 1, "gl": 1}
    )
    if not locale_data:
        raise HTTPException(status_code=404, detail=f"No locale details found for {locale}")

    google_domain = locale_data.get("google_domain", "google.com")
    hl = locale_data.get("hl", "en")
    gl = locale_data.get("gl", "us")

    params = {
        "api_key": SCALE_SERP_API_KEY,
        "search_type": "shopping",
        "q": query,
        "google_domain": google_domain,
        "hl": hl,
        "gl": gl,
        "shopping_condition": "new",
        "num": 1,
        "output": "json"
    }

    try:
        response = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)

        if response.status_code == 200:
            data = response.json()
            # ✅ Limit shopping_results to 1 (if exists)
            if "shopping_results" in data and isinstance(data["shopping_results"], list):
                data["shopping_results"] = data["shopping_results"][:1]
            return data

        raise HTTPException(
            status_code=response.status_code,
            detail=f"ScaleSERP API error {response.status_code}: {response.text}"
        )

    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Error reaching ScaleSERP API: {str(e)}")
