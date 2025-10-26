# master_sku_router.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any
import requests
import os
from datetime import datetime, timezone
from pymongo import MongoClient
from dotenv import load_dotenv

from utils.dependencies import verify_token
from utils.common import embed_query, find_best_match, category_embeddings, device_categories

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["SKU"]
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]

locale_collection = db["Locale_Params"]
master_collection = db["MasterSKU"]
failed_matches_collection = db["Failed_Matches"]

ICECAT_USERNAME = os.getenv("ICECAT_USER")
GO_UPC_API_KEY = os.getenv("GO_UPC_TOKEN")
SCALE_SERP_API_KEY = os.getenv("SCALE_SERP_KEY")


def utc_now_iso():
    """Returns the current UTC time in ISO 8601 format with 'Z' suffix and milliseconds."""
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MasterSKURequest(BaseModel):
    Make: str
    Model: str
    GTIN: str
    locale: str
    Category: Optional[str] = None


# --- Helper Functions ---

def is_valid_gtin(gtin: str) -> bool:
    """Checks if GTIN is valid."""
    return gtin.isdigit() and len(gtin) in {8, 12, 13, 14}

def fetch_locale_info(locale: str) -> Optional[Dict[str, Any]]:
    """Get locale info from database."""
    return locale_collection.find_one({"locale": locale}, {"_id": 0, "google_domain": 1, "hl": 1, "gl": 1, "currency": 1})

def fetch_icecat_by_gtin(gtin: str, locale: str) -> Optional[Dict]:
    """Try to get Icecat info by GTIN."""
    try:
        url = f"https://live.icecat.biz/api/?username={ICECAT_USERNAME}&lang={locale[:2]}&GTIN={gtin}"
        res = requests.get(url)
        if res.status_code == 200:
            return res.json().get("data", {})
    except Exception as e:
        print(f"[ICECAT GTIN error] {e}")
    return None

def fetch_icecat_by_make_model(make: str, model: str, locale: str) -> Optional[Dict]:
    """Try to get Icecat info by Make+Model."""
    try:
        url = f"https://live.icecat.biz/api/?username={ICECAT_USERNAME}&lang={locale[:2]}&brand={make}&productcode={model}"
        res = requests.get(url)
        if res.status_code == 200:
            return res.json().get("data", {})
    except Exception as e:
        print(f"[ICECAT fallback error] {e}")
    return None

def fetch_upc(gtin: str) -> Optional[Dict]:
    """Get product info from Go-UPC."""
    try:
        headers = {"Authorization": f"Bearer {GO_UPC_API_KEY}"}
        res = requests.get(f"https://go-upc.com/api/v1/code/{gtin}", headers=headers)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"[Go-UPC error] {e}")
    return None

def extract_make_model_from_title(title: str, data: MasterSKURequest):
    """Use OpenAI to extract Make and Model if missing."""
    if data.Make.strip() and data.Model.strip():
        return
    try:
        from openai import OpenAI
        openai = OpenAI()
        prompt = f"Extract the brand (Make) and model number from this product title: '{title}'. Return as JSON with keys 'Make' and 'Model'."
        response = openai.chat.completions.create(
            model="gpt-5-nano",
            messages=[{"role": "user", "content": prompt}]
        )
        import json
        parsed = json.loads(response.choices[0].message.content)
        if not data.Make.strip():
            data.Make = parsed.get("Make", "").strip()
        if not data.Model.strip():
            data.Model = parsed.get("Model", "").strip()
    except Exception as e:
        print(f"[GPT extraction error] {e}")

def extract_multimedia_urls(icecat_data: dict) -> dict:
    """
    Extracts 'manual_url' and 'product_fiche_url' from Icecat Multimedia list, if present.
    """
    multimedia = icecat_data.get("Multimedia") if icecat_data else None
    manual_url = None
    product_fiche_url = None

    if isinstance(multimedia, list):
        for item in multimedia:
            # Match both on "Type" and Description for robustness
            type_val = (item.get("Type") or "").lower()
            desc_val = (item.get("Description") or "").lower()
            url = item.get("URL")
            if not url:
                continue

            if "manual" in type_val or "manual" in desc_val:
                manual_url = url
            elif "fiche" in type_val or "fiche" in desc_val:
                product_fiche_url = url

    return {
        "manual_url": manual_url,
        "product_fiche_url": product_fiche_url
    }

def build_locale_data_from_serp(title: str, locale: str, category: str, model: str, extra: dict = None) -> Dict:
    locale_info = fetch_locale_info(locale)
    if not locale_info:
        raise HTTPException(status_code=404, detail=f"No locale details found for {locale}")

    currency_code = locale_info.get("currency")  # Get from Locale_Params

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

        block = {
            "locale": locale,
            "Category": category,
            "Input_Title": title,
            "SERP_Title": result.get("title"),
            "Google_ID": result.get("id"),
            "Google_URL": result.get("link"),
            "Merchant": result.get("merchant"),
            "Currency": currency_code,  # Set from Locale_Params
            "Price": result.get("price"),
            "MSRP_Source": "SERP" if result else "No SERP Match Found",
            "created_at": utc_now_iso()
        }
        if extra:
            block.update(extra)
        return block

    except Exception as e:
        print(f"[SERP error] {e}")
        block = {
            "locale": locale,
            "Category": category,
            "Input_Title": title,
            "SERP_Title": None,
            "Google_ID": None,
            "Google_URL": None,
            "Merchant": None,
            "Currency": currency_code,  # Set from Locale_Params
            "Price": None,
            "MSRP_Source": "No SERP Match Found",
            "created_at": utc_now_iso()
        }
        if extra:
            block.update(extra)
        return block

def get_existing_sku(data: MasterSKURequest) -> Optional[Dict]:
    """Find existing SKU by GTIN or Make+Model."""
    existing = None
    if data.GTIN.strip():
        existing = master_collection.find_one({"GTIN": {"$in": [data.GTIN]}})
    if not existing and data.Make.strip() and data.Model.strip():
        existing = master_collection.find_one({
            "Make": {"$regex": f"^{data.Make}$", "$options": "i"},
            "Model": {"$regex": data.Model, "$options": "i"}
        })
    return existing

def update_existing_sku(existing: Dict, data: MasterSKURequest, locale_block: Dict):
    """Update SKU with new locale-specific data and GTIN."""
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
    existing.setdefault("Locale_Specific_Data", []).append(locale_block)
    return existing

def get_category_for_embedding(data: MasterSKURequest, icecat_data: Optional[Dict], upc_data: Optional[Dict]) -> str:
    """Determine final category for embedding/matching/root, from all sources."""
    return (
        data.Category
        or (icecat_data.get("GeneralInfo", {}).get("Category", {}).get("Name", {}).get("Value") if icecat_data else None)
        or (upc_data.get("product", {}).get("category") if upc_data else None)
        or "Unknown"
    )

def choose_locale_category(icecat_data, upc_data, data):
    # 1. Try Icecat
    if icecat_data:
        cat = icecat_data.get("GeneralInfo", {}).get("Category", {}).get("Name", {}).get("Value")
        if cat: return cat
    # 2. Try UPC
    if upc_data:
        cat = upc_data.get("product", {}).get("category")
        if cat: return cat
    # 3. Try API input
    if data.Category and data.Category.strip():
        return data.Category.strip()
    # 4. Default to Unknown
    return "Unknown"

def get_image_and_brand(icecat_data: Optional[Dict], upc_data: Optional[Dict], data: MasterSKURequest):
    """Extract image and brand information."""
    image_url, brand_logo = None, None
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
    return image_url, brand_logo

def get_gtin_from_icecat(icecat_data: Optional[Dict], default_gtin: str):
    """Get GTINs from Icecat data, fallback to provided."""
    if isinstance(icecat_data, dict):
        general_info = icecat_data.get("GeneralInfo", {})
        gtin_data = general_info.get("GTIN")
        if isinstance(gtin_data, list):
            return gtin_data
    return [default_gtin]

def compute_category_embedding(category_input: str):
    embedding = embed_query(category_input)
    matched_category, similarity = find_best_match(embedding, category_embeddings, device_categories)
    final_category = matched_category if similarity >= 0.35 else "Unknown"
    return final_category, matched_category, similarity, embedding

def log_failed_match(category_input: str, data: MasterSKURequest, embedding, similarity: float):
    failed_doc = {
        "category_input": category_input,
        "Make": data.Make,
        "Model": data.Model,
        "GTIN": data.GTIN,
        "locale": data.locale,
        "embedding": embedding.tolist() if hasattr(embedding, 'tolist') else embedding,
        "similarity": similarity,
        "created_at": utc_now_iso(),
        "input_payload": data.dict()
    }
    failed_matches_collection.insert_one(failed_doc)

def add_serp_match_flag(locale_block: Dict, model: str):
    serp_title = locale_block.get("SERP_Title")
    if serp_title:
        model_lower = (model or "").lower()
        serp_title_lower = serp_title.lower()
        locale_block["Serp_match"] = model_lower in serp_title_lower
    else:
        locale_block["Serp_match"] = False

# --- Main Endpoint ---

@router.post("/create_master_sku")
def create_master_sku(data: MasterSKURequest, addSERP: Optional[bool] = False, _: None = Depends(verify_token)):
    # Validate
    if data.GTIN.strip() and not is_valid_gtin(data.GTIN):
        raise HTTPException(status_code=400, detail="Invalid GTIN format")

    if not fetch_locale_info(data.locale):
        raise HTTPException(status_code=404, detail=f"No locale data found for {data.locale}")

    existing = get_existing_sku(data)

    if existing:
        # Update with new locale data if not already present
        for entry in existing.get("Locale_Specific_Data", []):
            if entry.get("locale") == data.locale:
                existing["_id"] = str(existing["_id"])
                return {
                    "source": "master",
                    "matched_by": "GTIN or Make+Model",
                    "result": existing
                }

        # Fetch localized Icecat info if possible
        localized_title = f"{data.Make} {data.Model}"
        icecat_data_locale = fetch_icecat_by_gtin(data.GTIN, data.locale)
        if icecat_data_locale:
            localized_title = icecat_data_locale.get("GeneralInfo", {}).get("Title") or localized_title

        # New logic for locale-specific category
        upc_data_locale = fetch_upc(data.GTIN)
        locale_category_for_block = choose_locale_category(icecat_data_locale, upc_data_locale, data)

        extra = extract_multimedia_urls(icecat_data_locale) if icecat_data_locale else {}
        if addSERP:
            locale_block = build_locale_data_from_serp(localized_title, data.locale, locale_category_for_block, data.Model, extra=extra)
            add_serp_match_flag(locale_block, data.Model)
            # mark master serp status as completed for this locale
            try:
                master_collection.update_one({"_id": existing["_id"]}, {"$set": {"serp_status": "completed", "serp_last_updated": utc_now_iso()}})
            except Exception:
                pass
        else:
            locale_info = fetch_locale_info(data.locale) or {}
            currency_code = locale_info.get("currency")
            locale_block = {
                "locale": data.locale,
                "Category": locale_category_for_block,
                "Input_Title": localized_title,
                "SERP_Title": None,
                "Google_ID": None,
                "Google_URL": None,
                "Merchant": None,
                "Currency": currency_code,
                "Price": None,
                "MSRP_Source": "Skipped",
                "created_at": utc_now_iso(),
                "serp_pending": False,
                "Serp_match": False
            }
            if extra:
                locale_block.update(extra)

        updated = update_existing_sku(existing, data, locale_block)
        # ensure returned document reflects serp_status
        try:
            status_val = "completed" if addSERP else "skipped"
            master_collection.update_one({"_id": existing["_id"]}, {"$set": {"serp_status": status_val}})
            existing["serp_status"] = status_val
        except Exception:
            pass
        updated["_id"] = str(updated["_id"])
        return {
            "source": "master-update",
            "updated_locale": data.locale,
            "result": updated
        }

    # No existing match: Gather info from APIs
    icecat_data = fetch_icecat_by_gtin(data.GTIN, data.locale)
    title = f"{data.Make} {data.Model}"
    upc_data = None

    if icecat_data:
        title = icecat_data.get("GeneralInfo", {}).get("Title") or title
    else:
        icecat_data = fetch_icecat_by_make_model(data.Make, data.Model, data.locale)
        if icecat_data:
            title = icecat_data.get("GeneralInfo", {}).get("Title") or title

    if not icecat_data:
        upc_data = fetch_upc(data.GTIN)
        if upc_data:
            title = upc_data.get("product", {}).get("name") or title
            extract_make_model_from_title(title, data)

    # --- ROOT-LEVEL CATEGORY LOGIC FOR EMBEDDING ---
    category_input = get_category_for_embedding(data, icecat_data, upc_data)
    final_category, matched_category, similarity, embedding = compute_category_embedding(category_input)

    if final_category == "Unknown":
        # Log failed match
        log_failed_match(category_input, data, embedding, similarity)
        # If user did not provide any category, error
        if not (data.Category and data.Category.strip()):
            raise HTTPException(status_code=422, detail="No category could be matched, please provide input")

    gtin_from_icecat = get_gtin_from_icecat(icecat_data, data.GTIN)
    image_url, brand_logo = get_image_and_brand(icecat_data, upc_data, data)

    # --- PER-LOCALE CATEGORY LOGIC ---
    locale_category_for_block = choose_locale_category(icecat_data, upc_data, data)
    extra = extract_multimedia_urls(icecat_data) if icecat_data else {}
    if addSERP:
        locale_block = build_locale_data_from_serp(title, data.locale, locale_category_for_block, data.Model, extra=extra)
        add_serp_match_flag(locale_block, data.Model)
        serp_status_val = "completed"
    else:
        serp_status_val = "skipped"
        locale_info = fetch_locale_info(data.locale) or {}
        currency_code = locale_info.get("currency")
        locale_block = {
            "locale": data.locale,
            "Category": locale_category_for_block,
            "Input_Title": title,
            "SERP_Title": None,
            "Google_ID": None,
            "Google_URL": None,
            "Merchant": None,
            "Currency": currency_code,
            "Price": None,
            "MSRP_Source": "Skipped",
            "created_at": utc_now_iso(),
            "serp_pending": False,
            "Serp_match": False
        }
        if extra:
            locale_block.update(extra)

    now_iso = utc_now_iso()
    doc = {
        "created_at": now_iso,
        "Make": data.Make,
        "Model": data.Model,
        "Productname": data.Model,
        "GTIN": gtin_from_icecat,
        "Category": final_category,  # <--- for embeddings/matching
        "Matched_Category": matched_category,
        "Match_Similarity": similarity,
        "Title": title,
        "Image_URL": image_url,
        "brand_logo": brand_logo,
        "Source": "CAT" if icecat_data else ("UPC" if upc_data else "INPUT"),
        "Locale_Specific_Data": [locale_block],
        "serp_status": serp_status_val
    }

    result = master_collection.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc
