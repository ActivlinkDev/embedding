"""Create and enrich canonical v2 MasterSKU documents."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
import httpx
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
import requests

from services.catalog import SCHEMA_VERSION, master_match_key, normalize_text, serialize, utc_now
from utils.dependencies import verify_token
from .catalog_dependencies import catalog, database, master_collection, locale_collection


router = APIRouter(prefix="/sku", tags=["Catalog"])
logger = logging.getLogger(__name__)
category_collection = database["Category"]
url_map_collection = database["url_map"]

ICECAT_USERNAME = os.getenv("ICECAT_USER")
GO_UPC_API_KEY = os.getenv("GO_UPC_TOKEN")


class MasterSKURequest(BaseModel):
    Make: str = Field("")
    Model: str = Field("")
    GTIN: str = Field("")
    locale: str
    Category: Optional[str] = None


def is_valid_gtin(gtin: str) -> bool:
    return gtin.isdigit() and len(gtin) in {8, 12, 13, 14}


def _icecat(gtin: str, make: str, model: str, locale: str) -> dict:
    if not ICECAT_USERNAME:
        return {}
    params = {"username": ICECAT_USERNAME, "lang": locale[:2]}
    if gtin:
        params["GTIN"] = gtin
    elif make and model:
        params.update({"brand": make, "productcode": model})
    else:
        return {}
    try:
        response = requests.get("https://live.icecat.biz/api/", params=params, timeout=20)
        return response.json().get("data", {}) if response.ok else {}
    except Exception:
        logger.exception("Icecat lookup failed")
        return {}


def _go_upc(gtin: str) -> dict:
    if not gtin or not GO_UPC_API_KEY:
        return {}
    try:
        response = requests.get(
            f"https://go-upc.com/api/v1/code/{gtin}",
            headers={"Authorization": f"Bearer {GO_UPC_API_KEY}"},
            timeout=20,
        )
        return response.json() if response.ok else {}
    except Exception:
        logger.exception("Go-UPC lookup failed")
        return {}


def _extract_product_data(data: MasterSKURequest) -> dict:
    icecat = _icecat(data.GTIN.strip(), data.Make.strip(), data.Model.strip(), data.locale)
    upc = _go_upc(data.GTIN.strip()) if not icecat else {}
    general = icecat.get("GeneralInfo") or {}
    upc_product = upc.get("product") or {}

    brand = general.get("Brand") or general.get("BrandName") or upc_product.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("Value") or brand.get("Name")
    product_name = general.get("ProductName") or general.get("ProductCode")
    if isinstance(product_name, dict):
        product_name = product_name.get("Value")
    title = general.get("Title") or upc_product.get("name")
    if isinstance(title, dict):
        title = title.get("Value")
    raw_category = general.get("Category") or {}
    category = raw_category.get("Name") if isinstance(raw_category, dict) else raw_category
    if isinstance(category, dict):
        category = category.get("Value") or category.get("Name")
    category = data.Category or category or upc_product.get("category") or ""

    image_url = general.get("CoverPicture") or upc_product.get("imageUrl")
    if isinstance(image_url, dict):
        image_url = image_url.get("Pic") or image_url.get("Value")

    gtins = [data.GTIN.strip()] if data.GTIN.strip() else []
    icecat_gtins = general.get("GTIN")
    if isinstance(icecat_gtins, list):
        for item in icecat_gtins:
            value = item.get("Value") if isinstance(item, dict) else item
            if value and str(value) not in gtins:
                gtins.append(str(value))

    media = icecat.get("Multimedia") if isinstance(icecat.get("Multimedia"), list) else []
    return {
        "make": str(data.Make or brand or "").strip(),
        "model": str(data.Model or product_name or "").strip(),
        "gtins": gtins,
        "title": str(title or "").strip(),
        "category": str(category or "").strip(),
        "imageUrl": image_url,
        "media": media,
    }


def _masked_url(url: str, base_url: str) -> str:
    key = str(uuid4())
    url_map_collection.insert_one({
        "_id": key,
        "url": url,
        "created_at": utc_now(),
        "expires_at": utc_now() + timedelta(days=365),
    })
    return f"{base_url.rstrip('/')}/sku/r/{key}"


def _assets(product: dict, base_url: str) -> dict:
    assets: dict[str, Any] = {}
    if product.get("imageUrl"):
        assets["primaryImage"] = product["imageUrl"]
    documents = []
    for item in product.get("media") or []:
        if not isinstance(item, dict) or not item.get("URL"):
            continue
        documents.append({
            "label": str(item.get("Type") or item.get("Description") or "document"),
            "url": _masked_url(str(item["URL"]), base_url),
            "contentType": item.get("ContentType"),
        })
    if documents:
        assets["documents"] = documents
    return assets


async def _run_dseo_task(locale: str, masterSKUid: str):
    try:
        from routers.enrich.dseo_shopping import submit_dseo_shopping_task
        await submit_dseo_shopping_task(masterSKUid=masterSKUid, locale=locale)
    except Exception:
        logger.exception("Failed to schedule MasterSKU market enrichment")


def create_master_sku_service(
    data: MasterSKURequest,
    background_tasks: BackgroundTasks,
    request: Optional[Request] = None,
    add_pricing: bool = True,
) -> dict:
    gtin = data.GTIN.strip()
    if gtin and not is_valid_gtin(gtin):
        raise HTTPException(status_code=400, detail="Invalid GTIN format")
    if not gtin and not (data.Make.strip() and data.Model.strip()):
        raise HTTPException(status_code=400, detail="Provide GTIN or Make and Model")

    locale_info = locale_collection.find_one({"locale": data.locale})
    if not locale_info:
        raise HTTPException(status_code=404, detail=f"Locale {data.locale} not found")

    existing, matched_by = catalog.find_master(
        gtin=gtin or None,
        make=data.Make or None,
        model=data.Model or None,
    )
    if existing and data.locale in (existing.get("locales") or {}):
        if add_pricing:
            background_tasks.add_task(_run_dseo_task, data.locale, str(existing["_id"]))
        return {"source": "master", "matchedBy": matched_by, "masterSku": serialize(existing)}

    product = _extract_product_data(data)
    if existing:
        existing_identifiers = existing.get("identifiers") or {}
        product["make"] = product["make"] or existing_identifiers.get("make") or ""
        product["model"] = product["model"] or existing_identifiers.get("model") or ""
        product["gtins"] = product["gtins"] or list(existing_identifiers.get("gtins") or [])
        product["category"] = product["category"] or existing.get("category") or ""
        product["imageUrl"] = product["imageUrl"] or existing.get("imageUrl")
    if not product["make"] or not product["model"]:
        raise HTTPException(
            status_code=422,
            detail="Product make and model could not be resolved; provide both values",
        )
    if not product["category"]:
        raise HTTPException(
            status_code=422,
            detail="Product category could not be resolved; provide Category",
        )
    base_url = str(request.base_url).rstrip("/") if request else os.getenv("FASTAPI_BASE_URL", "")
    key = master_match_key(product["make"], product["model"], product["gtins"])
    now = utc_now()
    locale_block = {
        "title": product["title"] or f"{product['make']} {product['model']}".strip(),
        "category": product["category"],
        "market": {
            "referencePrice": None,
            "currency": locale_info.get("currency", ""),
            "merchant": None,
        },
        "assets": _assets(product, base_url) if base_url else {},
        "specifications": {},
        "enrichment": {"status": "pending" if add_pricing else "not_requested"},
        "createdAt": now,
        "updatedAt": now,
    }
    set_on_insert = {
        "schemaVersion": SCHEMA_VERSION,
        "matchKey": key,
        "identifiers": {
            "make": product["make"],
            "makeNormalized": normalize_text(product["make"]),
            "model": product["model"],
            "modelNormalized": normalize_text(product["model"]),
            "gtins": product["gtins"],
        },
        "category": product["category"],
        "imageUrl": product.get("imageUrl"),
        "provenance": {"createdFrom": "catalog-api"},
        "createdAt": now,
    }
    master_filter = {"_id": existing["_id"]} if existing else {"matchKey": key}
    saved = master_collection.find_one_and_update(
        master_filter,
        {
            "$setOnInsert": set_on_insert,
            "$set": {f"locales.{data.locale}": locale_block, "updatedAt": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    if saved and product["gtins"]:
        master_collection.update_one(
            {"_id": saved["_id"]},
            {"$addToSet": {"identifiers.gtins": {"$each": product["gtins"]}}},
        )
        saved = master_collection.find_one({"_id": saved["_id"]})
    if not saved:
        raise HTTPException(status_code=500, detail="Failed to create MasterSKU")
    if add_pricing:
        background_tasks.add_task(_run_dseo_task, data.locale, str(saved["_id"]))
    return {
        "source": "master-update" if existing else "master-create",
        "matchedBy": matched_by,
        "masterSku": serialize(saved),
    }


@router.post("/create_master_sku")
def create_master_sku(
    data: MasterSKURequest,
    request: Request,
    background_tasks: BackgroundTasks,
    add_pricing: bool = Query(True),
    _: None = Depends(verify_token),
):
    return create_master_sku_service(data, background_tasks, request, add_pricing)


@router.get("/r/{key}")
async def proxy_masked(key: str):
    doc = url_map_collection.find_one({"_id": key})
    if not doc or not doc.get("url"):
        raise HTTPException(status_code=404, detail="Not found")
    expires_at = doc.get("expires_at")
    if isinstance(expires_at, datetime):
        comparable = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if comparable < datetime.now(timezone.utc):
            raise HTTPException(status_code=404, detail="Not found")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(doc["url"], follow_redirects=True)
            headers = {
                name: value for name, value in response.headers.items()
                if name.lower() not in {"connection", "transfer-encoding", "content-encoding"}
            }
            body = await response.aread()
        return StreamingResponse(
            iter([body]),
            status_code=response.status_code,
            headers=headers,
            media_type=response.headers.get("content-type"),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {exc}")


@router.get("/test-background")
async def test_background(
    masterSKUid: str = Query(...),
    locale: str = Query("en_GB"),
    _: None = Depends(verify_token),
):
    asyncio.create_task(_run_dseo_task(locale, masterSKUid))
    return {"status": "scheduled"}
