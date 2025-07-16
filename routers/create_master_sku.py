from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import requests
import os
from datetime import datetime
from pymongo import MongoClient
from dotenv import load_dotenv
from utils.dependencies import verify_token
from utils.common import embed_query, find_best_match, category_embeddings, device_categories

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Create Master SKU"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]

locale_collection = db["Locale_Params"]
master_collection = db["MasterSKU"]

ICECAT_USERNAME = os.getenv("ICECAT_USER")
GO_UPC_API_KEY = os.getenv("GO_UPC_TOKEN")
SCALE_SERP_API_KEY = os.getenv("SCALE_SERP_KEY")


class MasterSKURequest(BaseModel):
    Make: str
    Model: str
    GTIN: str
    locale: str
    Category: Optional[str] = None

def is_valid_gtin(gtin: str) -> bool:
    return gtin.isdigit() and len(gtin) in {8, 12, 13, 14}

def build_locale_data_from_serp(title: str, locale: str, category: str, model: str):
    locale_info = locale_collection.find_one(
        {"locale": locale},
        {"_id": 0, "google_domain": 1, "hl": 1, "gl": 1}
    )
    if not locale_info:
        raise HTTPException(status_code=404, detail=f"No locale details found for {locale}")

    params = {
        "api_key": SCALE_SERP_API_KEY,
        "search_type": "shopping",
        "q": title,
        "google_domain": locale_info.get("google_domain", "google.com"),
        "hl": locale_info.get("hl", "en"),
        "gl": locale_info.get("gl", "us"),
        "shopping_condition": "new",
        "num": 1,
        "output": "json"
    }

    try:
        response = requests.get("https://api.scaleserp.com/search", params=params, timeout=10)
        data = response.json()

        result = data.get("shopping_results", [{}])[0] if data.get("shopping_results") else {}

        return {
            "locale": locale,
            "Category": category,
            "Input_Title": title,
            "SERP_Title": result.get("title"),
            "Google_ID": result.get("id"),
            "Google_URL": result.get("link"),
            "Merchant": result.get("merchant"),
            "Currency": result.get("price_parsed", {}).get("currency"),
            "Price": result.get("price"),
            "MSRP_Source": "SERP" if result else "No SERP Match Found",
            "created_at": datetime.utcnow().isoformat()
        }

    except Exception as e:
        print(f"[SERP error] {e}")
        return {
            "locale": locale,
            "Category": category,
            "Input_Title": title,
            "SERP_Title": None,
            "Google_ID": None,
            "Google_URL": None,
            "Merchant": None,
            "Currency": None,
            "Price": None,
            "MSRP_Source": "No SERP Match Found",
            "created_at": datetime.utcnow().isoformat()
        }

@router.post("/create_master_sku")
def create_master_sku(
    data: MasterSKURequest,
    _: None = Depends(verify_token)
):
    if data.GTIN.strip() and not is_valid_gtin(data.GTIN):
        raise HTTPException(status_code=400, detail="Invalid GTIN format")

    locale_data = locale_collection.find_one({"locale": data.locale}, {"_id": 0})
    if not locale_data:
        raise HTTPException(status_code=404, detail=f"No locale data found for {data.locale}")

    existing = None
    if data.GTIN.strip():
        existing = master_collection.find_one({"GTIN": {"$in": [data.GTIN]}})

    if not existing and data.Make.strip() and data.Model.strip():
        existing = master_collection.find_one({
            "Make": {"$regex": f"^{data.Make}$", "$options": "i"},
            "Model": {"$regex": data.Model, "$options": "i"}
        })

    if existing:
        for entry in existing.get("Locale_Specific_Data", []):
            if entry.get("locale") == data.locale:
                existing["_id"] = str(existing["_id"])
                return {
                    "source": "master",
                    "matched_by": "GTIN or Make+Model",
                    "result": existing
                }

        localized_title = f"{data.Make} {data.Model}"
        icecat_data_locale = None
        try:
            url = f"https://live.icecat.biz/api/?username={ICECAT_USERNAME}&lang={data.locale[:2]}&GTIN={data.GTIN}"
            res = requests.get(url)
            if res.status_code == 200:
                icecat_data_locale = res.json().get("data", {})
                localized_title = icecat_data_locale.get("GeneralInfo", {}).get("Title") or localized_title
        except Exception as e:
            print(f"[ICECAT locale fetch error] {e}")

        category_name = (
            icecat_data_locale.get("GeneralInfo", {}).get("Category", {}).get("Name", {}).get("Value")
            if icecat_data_locale else existing.get("Category", "Unknown")
        )

        locale_block = build_locale_data_from_serp(localized_title, data.locale, category_name, data.Model)

        master_collection.update_one(
            {"_id": existing["_id"]},
            {"$addToSet": {"GTIN": data.GTIN}}
        )
        master_collection.update_one(
            {"_id": existing["_id"]},
            {"$pull": {"Locale_Specific_Data": {"locale": data.locale}}}
        )
        master_collection.update_one(
            {"_id": existing["_id"]},
            {"$addToSet": {"Locale_Specific_Data": locale_block}}
        )

        existing["_id"] = str(existing["_id"])
        existing.setdefault("Locale_Specific_Data", []).append(locale_block)

        return {
            "source": "master-update",
            "updated_locale": data.locale,
            "result": existing
        }

    # No existing match, create new document
    icecat_data = None
    upc_data = None
    title = f"{data.Make} {data.Model}"

    try:
        url = f"https://live.icecat.biz/api/?username={ICECAT_USERNAME}&lang={data.locale[:2]}&GTIN={data.GTIN}"
        res = requests.get(url)
        if res.status_code == 200:
            raw = res.json()
            icecat_data = raw.get("data", {})
            title = icecat_data.get("GeneralInfo", {}).get("Title") or title
    except Exception as e:
        print(f"[ICECAT GTIN error] {e}")

    if not icecat_data:
        try:
            url = f"https://live.icecat.biz/api/?username={ICECAT_USERNAME}&lang={data.locale[:2]}&brand={data.Make}&productcode={data.Model}"
            res = requests.get(url)
            if res.status_code == 200:
                raw = res.json()
                icecat_data = raw.get("data", {})
                title = icecat_data.get("GeneralInfo", {}).get("Title") or title
        except Exception as e:
            print(f"[ICECAT fallback error] {e}")

    if not icecat_data:
        try:
            headers = {"Authorization": f"Bearer {GO_UPC_API_KEY}"}
            res = requests.get(f"https://go-upc.com/api/v1/code/{data.GTIN}", headers=headers)
            if res.status_code == 200:
                upc_data = res.json()
                title = upc_data.get("product", {}).get("name") or title

                if not data.Make.strip() or not data.Model.strip():
                    from openai import OpenAI
                    openai = OpenAI()
                    prompt = f"Extract the brand (Make) and model number from this product title: '{title}'. Return as JSON with keys 'Make' and 'Model'."
                    try:
                        response = openai.chat.completions.create(
                            model="gpt-3.5-turbo",
                            messages=[{"role": "user", "content": prompt}]
                        )
                        import json
                        parsed = json.loads(response.choices[0].message.content)
                        data.Make = data.Make or parsed.get("Make", "")
                        data.Model = data.Model or parsed.get("Model", "")
                    except Exception as e:
                        print(f"[GPT extraction error] {e}")
        except Exception as e:
            print(f"[Go-UPC error] {e}")

    category_input = (
        data.Category
        or (icecat_data.get("GeneralInfo", {}).get("Category", {}).get("Name", {}).get("Value") if icecat_data else None)
        or (upc_data.get("product", {}).get("category") if upc_data else None)
        or "Unknown"
    )

    embedding = embed_query(category_input)
    matched_category, similarity = find_best_match(embedding, category_embeddings, device_categories)
    final_category = matched_category if similarity >= 0.35 else "Unknown"

    now_iso = datetime.utcnow().isoformat()
    gtin_from_icecat = [data.GTIN]
    if isinstance(icecat_data, dict):
        general_info = icecat_data.get("GeneralInfo", {})
        gtin_data = general_info.get("GTIN")
        if isinstance(gtin_data, list):
            gtin_from_icecat = gtin_data

    image_url = None
    brand_logo = None

    if icecat_data:
        info = icecat_data.get("GeneralInfo", {})
        image_url = icecat_data.get("Image", {}).get("HighPic")
        brand_logo = info.get("BrandLogo") or info.get("BrandInfo", {}).get("BrandLogo")
        brand = info.get("Brand")
        if isinstance(brand, dict):
            data.Make = brand.get("Value", data.Make)
        elif isinstance(brand, str):
            data.Make = brand or data.Make
        name_info = info.get("ProductNameInfo", {}).get("ProductIntName")
        if isinstance(name_info, dict):
            data.Model = name_info.get("Value", data.Model)
        elif isinstance(name_info, str):
            data.Model = name_info or data.Model
    elif upc_data:
        image_url = upc_data.get("product", {}).get("imageUrl")

    locale_category_for_block = category_input if icecat_data else final_category
    locale_block = build_locale_data_from_serp(title, data.locale, locale_category_for_block, data.Model)

    doc = {
        "created_at": now_iso,
        "Make": data.Make,
        "Model": data.Model,
        "Productname": data.Model,
        "GTIN": gtin_from_icecat,
        "Category": final_category,
        "Matched_Category": matched_category,
        "Match_Similarity": similarity,
        "Title": title,
        "Image_URL": image_url,
        "brand_logo": brand_logo,
        "Source": "ICECAT" if icecat_data else ("UPC" if upc_data else "INPUT"),
        "Locale_Specific_Data": [locale_block]
    }

    result = master_collection.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc
