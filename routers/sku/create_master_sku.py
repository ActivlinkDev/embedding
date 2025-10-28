# master_sku_router.py

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional, Dict, Any
import requests
import threading
import asyncio
import os
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from pymongo import MongoClient
from pymongo import ReturnDocument, errors
import re
from dotenv import load_dotenv
from fastapi.responses import RedirectResponse, StreamingResponse
import httpx
import logging

from utils.dependencies import verify_token
from utils.common import embed_query, find_best_match, category_embeddings, device_categories

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["SKU"]
)


@router.get("/r/{key}")
async def proxy_masked(key: str):
    """Proxy endpoint for masked URLs stored in `url_map`.
    Fetches the upstream resource server-side and streams it back so the
    browser remains on the masked URL (upstream host is not exposed).
    """
    try:
        doc = url_map_collection.find_one({"_id": key})
    except Exception:
        doc = None
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    # Optional expiry check
    expires_at = doc.get("expires_at")
    if expires_at and isinstance(expires_at, datetime) and expires_at < datetime.utcnow():
        raise HTTPException(status_code=404, detail="Not found")

    url = doc.get("url")
    if not url:
        raise HTTPException(status_code=404, detail="Not found")

    # Stream the upstream response back to the client
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.get(url, follow_redirects=True)

            # Filter hop-by-hop headers
            hop_by_hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "content-encoding"}
            headers = {k: v for k, v in upstream.headers.items() if k.lower() not in hop_by_hop}

            media_type = upstream.headers.get("content-type")
            return StreamingResponse(upstream.aiter_bytes(), status_code=upstream.status_code, headers=headers, media_type=media_type)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {str(e)}")

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]

locale_collection = db["Locale_Params"]
master_collection = db["MasterSKU"]
failed_matches_collection = db["Failed_Matches"]
url_map_collection = db["url_map"]
background_jobs_collection = db.get("Background_Jobs") or db["Background_Jobs"]

# module logger
logger = logging.getLogger(__name__)
# If root logging isn't configured, default to INFO so we still see messages during dev.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)

# Ensure a uniqueness guard to reduce duplicate MasterSKU creation.
# Use a computed `match_key` (prefers GTIN when present, otherwise normalized make|model).
try:
    master_collection.create_index("match_key", unique=True, sparse=True)
except Exception:
    # If index creation fails (permissions, etc.) continue — DB-level dedupe unavailable.
    pass

# Ensure TTL index on url_map.expires_at if present (best-effort)
try:
    url_map_collection.create_index("expires_at", expireAfterSeconds=0)
except Exception:
    pass

ICECAT_USERNAME = os.getenv("ICECAT_USER")
GO_UPC_API_KEY = os.getenv("GO_UPC_TOKEN")
SCALE_SERP_API_KEY = os.getenv("SCALE_SERP_KEY")


def utc_now_iso():
    """Returns the current UTC time in ISO 8601 format with 'Z' suffix and milliseconds."""
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bg_call_scale_lookup(query: str, locale: str, masterSKUid: str, base_url: str = None):
    try:
        # If a base_url is provided, prefer calling the ScaleSERP endpoint over HTTP.
        # This avoids import/circular-import problems and dependency injection issues
        # that can arise when calling FastAPI endpoint functions directly in-process.
        if base_url and str(base_url).strip():
            try:
                url = str(base_url).rstrip('/') + "/scale/shopping"
                params = {"query": query, "locale": locale, "masterSKUid": masterSKUid}
                print(f"[bg_scale] calling ScaleSERP via HTTP {url} params={params}")
                # use requests (sync) inside this background thread
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code >= 400:
                    print(f"[bg_scale] HTTP scale lookup failed {resp.status_code}: {resp.text}")
                else:
                    print(f"[bg_scale] HTTP scale lookup succeeded for masterSKUid={masterSKUid}")
                return
            except Exception as e:
                print(f"[bg_scale] HTTP call to scale lookup failed: {e}")

        # Fallback: attempt in-process call to the async handler in a fresh event loop.
        # Keep original behavior for environments where HTTP access to the server
        # is not available (e.g., single-process testing).
        print(f"[bg_scale] running in-process scale lookup for masterSKUid={masterSKUid} query='{query}' locale={locale}")
        try:
            # local import to avoid circular imports at module load
            try:
                from routers.enrich.scale_lookup import get_shopping_result
            except Exception:
                try:
                    # alternative import path
                    from enrich.scale_lookup import get_shopping_result
                except Exception as ie:
                    print(f"[bg_scale] failed to import scale lookup: {ie}")
                    return

            # run the async endpoint function in a fresh event loop
            try:
                asyncio.run(get_shopping_result(query=query, locale=locale, masterSKUid=masterSKUid))
            except Exception as e:
                print(f"[bg_scale] error running get_shopping_result: {e}")
        except Exception as e:
            print(f"[background scale_lookup error] {e}")
    except Exception as e:
        print(f"[background scale_lookup error] {e}")


def schedule_scale_lookup_background(query: str, locale: str, masterSKUid: str, base_url: str = None):
    try:
        # Prefer FASTAPI_BASE_URL environment variable when available. This ensures
        # background lookups consistently target the configured backend URL even
        # if callers use request.base_url or omit the param.
        resolved_base = os.getenv("FASTAPI_BASE_URL") or base_url
        print(f"[schedule_scale_lookup_background] scheduling background lookup for masterSKUid={masterSKUid} query='{query}' locale={locale} base_url={resolved_base}")
        t = threading.Thread(target=_bg_call_scale_lookup, args=(query, locale, masterSKUid, resolved_base), daemon=True)
        t.start()
    except Exception as e:
        print(f"[schedule scale_lookup error] {e}")


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


def mask_and_store_url(url: str, base_url: str = None, ttl_seconds: int = None) -> str:
    """
    Create a mapping record in `url_map` and return a masked internal path.
    The mapping document has _id set to a UUID string and stores the original URL.
    Returns path like "/sku/r/<uuid>" which can be used in MasterSKU documents.
    """
    try:
        key = str(uuid4())
        doc = {"_id": key, "url": url, "created_at": datetime.utcnow()}
        if ttl_seconds and isinstance(ttl_seconds, int):
            doc["expires_at"] = datetime.utcnow() + timedelta(seconds=ttl_seconds)
        url_map_collection.insert_one(doc)
        # Use provided base_url, then configured env, then fallback to localhost:8000
        base = (base_url and str(base_url).strip()) or os.getenv("FASTAPI_BASE_URL") or os.getenv("PUBLIC_BACKEND_URL") or "http://localhost:8000"
        return base.rstrip('/') + f"/sku/r/{key}"
    except Exception:
        # On any failure, fall back to storing the original url (safer than dropping it)
        return url


def _mask_extra_urls(extra: dict, base_url: str = None) -> dict:
    """Replace any Icecat manual/product_fiche URLs in `extra` with masked paths."""
    if not extra or not isinstance(extra, dict):
        return extra
    out = dict(extra)
    try:
        for k in ("manual_url", "product_fiche_url"):
            if out.get(k):
                try:
                    out[k] = mask_and_store_url(out[k], base_url=base_url)
                except Exception:
                    # leave original if masking fails
                    pass
    except Exception:
        return extra
    return out

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
    final_category = matched_category if similarity >= 0.49 else "Unknown"
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
def create_master_sku(data: MasterSKURequest, request: Request, addSERP: Optional[bool] = False, _: None = Depends(verify_token)):
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
        # Compute base for masked links: prefer env, else derive from incoming request
        base_for_mask = os.getenv("FASTAPI_BASE_URL") or str(request.base_url).rstrip('/')
        # Mask any Icecat URLs so we don't persist raw upstream links
        extra = _mask_extra_urls(extra, base_url=base_for_mask)
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
                "Merchant": None,
                "Currency": currency_code,
                "Price": None,
                "MSRP_Source": "Skipped",
                "created_at": utc_now_iso()
            }
            if extra:
                locale_block.update(extra)

        # Ensure existing doc has a match_key so future upserts can find it atomically.
        try:
            if data.GTIN and data.GTIN.strip():
                mk = f"gtin:{data.GTIN.strip()}"
            else:
                mk = f"mm:{(data.Make or '').strip().lower()}|{(data.Model or '').strip().lower()}"
            master_collection.update_one({"_id": existing["_id"]}, {"$set": {"match_key": mk}})
            existing["match_key"] = mk
        except Exception:
            # best-effort only
            pass

        updated = update_existing_sku(existing, data, locale_block)
        # ensure returned document reflects serp_status
        try:
            status_val = "completed" if addSERP else "skipped"
            master_collection.update_one({"_id": existing["_id"]}, {"$set": {"serp_status": status_val}})
            existing["serp_status"] = status_val
        except Exception:
            pass
        updated["_id"] = str(updated["_id"])
        # schedule background ScaleSERP lookup using Make+Model
        try:
            base_for_mask = os.getenv("FASTAPI_BASE_URL") or str(request.base_url).rstrip('/')
            schedule_scale_lookup_background(f"{data.Make} {data.Model}", data.locale, str(updated["_id"]), base_url=base_for_mask)
        except Exception:
            pass

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
    base_for_mask = os.getenv("FASTAPI_BASE_URL") or str(request.base_url).rstrip('/')
    # Mask any Icecat URLs so we don't persist raw upstream links
    extra = _mask_extra_urls(extra, base_url=base_for_mask)
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
            "Merchant": None,
            "Currency": currency_code,
            "Price": None,
            "MSRP_Source": "Skipped",
            "created_at": utc_now_iso()
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

    # Compute a stable match_key for this product (prefer GTIN when available)
    if data.GTIN and data.GTIN.strip():
        match_key = f"gtin:{data.GTIN.strip()}"
    else:
        match_key = f"mm:{(data.Make or '').strip().lower()}|{(data.Model or '').strip().lower()}"
    doc["match_key"] = match_key

    # Use an atomic upsert to avoid race-condition duplicate inserts.
    try:
        update = {
            "$setOnInsert": doc,
            # Ensure GTIN array contains any gtins we discovered
            "$addToSet": {"GTIN": {"$each": gtin_from_icecat}, "Locale_Specific_Data": locale_block}
        }
        res = master_collection.find_one_and_update({"match_key": match_key}, update, upsert=True, return_document=ReturnDocument.AFTER)
        # If the returned doc came from DB, ensure _id is a string
        if res:
            try:
                res["_id"] = str(res["_id"])
            except Exception:
                pass
            # schedule background ScaleSERP lookup for newly created/upserted MasterSKU
            try:
                base_for_mask = os.getenv("FASTAPI_BASE_URL") or str(request.base_url).rstrip('/')
                schedule_scale_lookup_background(f"{data.Make} {data.Model}", data.locale, str(res["_id"]), base_url=base_for_mask)
            except Exception:
                pass
            return res
    except errors.DuplicateKeyError:
        # Rare race: another process created the doc between our check and upsert. Fetch the existing doc.
        existing_doc = master_collection.find_one({"match_key": match_key})
        if existing_doc:
            try:
                existing_doc["_id"] = str(existing_doc["_id"])
            except Exception:
                pass
                # schedule a background lookup for the found existing doc as well
                try:
                    base_for_mask = os.getenv("FASTAPI_BASE_URL") or str(request.base_url).rstrip('/')
                    schedule_scale_lookup_background(f"{data.Make} {data.Model}", data.locale, str(existing_doc["_id"]), base_url=base_for_mask)
                except Exception:
                    pass
                return existing_doc
    except Exception as e:
        # As a fallback, attempt a plain insert (so we don't fail hard for unexpected DB errors)
        try:
            result = master_collection.insert_one(doc)
            doc["_id"] = str(result.inserted_id)
            return doc
        except Exception:
            raise HTTPException(status_code=500, detail=f"Failed to create MasterSKU: {str(e)}")
