"""Create and enrich canonical v2 MasterSKU documents.

This module exposes no routes of its own: masters are created as a side effect of
`create_custom_sku`, which calls `create_master_sku_service` directly, and by the EPREL
import. The `/sku/r/{key}` proxy that serves the documents written here lives in
`routers.sku.masked_asset_proxy`.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import os
from typing import Any, Optional
from uuid import uuid4

from fastapi import BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
import requests

from services.catalog import SCHEMA_VERSION, master_match_key, normalize_text, serialize, utc_now
from utils.category_tree import resolve as resolve_category
from .catalog_dependencies import catalog, database, master_collection, locale_collection


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


def compute_category_embedding(category_input: str):
    """Map enrichment text to the canonical Category taxonomy."""
    try:
        from utils.common import (
            category_embeddings,
            device_categories,
            embed_query,
            find_best_match,
            mongo_vector_search,
        )

        embedding = embed_query(category_input)
        matched_category, similarity = mongo_vector_search(embedding)
        if not matched_category:
            matched_category, similarity = find_best_match(
                embedding, category_embeddings, device_categories
            )
    except Exception:
        logger.exception("Category classification failed for input=%r", category_input)
        return "Unknown", None, 0.0, []

    final_category = (
        matched_category
        if matched_category and float(similarity or 0.0) >= 0.42
        else "Unknown"
    )
    return final_category, matched_category, float(similarity or 0.0), embedding


def _canonical_category(category_input: str, explicit_category: Optional[str]) -> str:
    if explicit_category and explicit_category.strip():
        return resolve_category(explicit_category)["category"] or explicit_category.strip()
    if not category_input.strip():
        return ""
    exact = resolve_category(category_input)
    if exact.get("group") or exact.get("sector"):
        return exact["category"] or ""
    matched, _, _, _ = compute_category_embedding(category_input)
    return "" if matched == "Unknown" else matched


def _icecat_value(*candidates: Any) -> str:
    """First non-empty Icecat field value.

    Icecat returns some fields as a bare string and others as a localised
    ``{"Value": ..., "Language": ...}`` object, sometimes both for the same key
    across products, so every read has to tolerate either shape.
    """
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("Value")
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _icecat_localised(language: str, *candidates: Any) -> str:
    """First non-empty candidate, preferring one tagged with the requested language.

    Icecat returns localised copy as ``{"Value": ..., "Language": "FR"}`` and silently
    falls back to another language when it holds none for the one requested, so a
    language-tagged value that does not match is only used when nothing better exists.
    """
    wanted = str(language or "").strip().upper()
    if wanted:
        for candidate in candidates:
            tag = candidate.get("Language") if isinstance(candidate, dict) else None
            if str(tag or "").strip().upper() not in ("", wanted):
                continue  # Icecat fell back to another language for this field
            text = _icecat_value(candidate)
            if text:
                return text
    return _icecat_value(*candidates)


def _icecat_description(icecat_general: dict) -> str:
    """Prose description for the product card.

    ``LongSummaryDescription`` reads as sentences; ``ShortSummaryDescription`` is a
    comma-separated spec dump. Prefer the former and fall back to the latter.
    """
    summary = icecat_general.get("SummaryDescription")
    if not isinstance(summary, dict):
        return ""
    return _icecat_value(
        summary.get("LongSummaryDescription"),
        summary.get("ShortSummaryDescription"),
    )


def _icecat_features(icecat_general: dict, language: str = "") -> list:
    """Bullet points, preferring Icecat's supplier-authored list over its generated one.

    Each block is language-tagged, so a block in the requested language wins over one
    Icecat fell back to, even if the fallback comes from the preferred source.
    """
    blocks = []
    for key in ("BulletPoints", "GeneratedBulletPoints"):
        block = icecat_general.get(key)
        if not isinstance(block, dict):
            continue
        values = block.get("Values")
        if not isinstance(values, list):
            continue
        bullets = [text for text in (_icecat_value(value) for value in values) if text]
        if bullets:
            blocks.append((str(block.get("Language") or "").strip().upper(), bullets))
    if not blocks:
        return []
    wanted = str(language or "").strip().upper()
    if wanted:
        for block_language, bullets in blocks:
            if block_language in ("", wanted):
                return bullets
    return blocks[0][1]


def _icecat_specifications(icecat: dict) -> dict:
    """Flatten FeaturesGroups into the {name: value} shape the product card renders.

    ``PresentationValue`` carries the unit ('147.3 cm (58")', '130 W'), so it is
    preferred over the raw value. Later groups do not overwrite earlier ones —
    Icecat orders groups by relevance and can repeat a feature name across them.
    """
    specs: dict[str, str] = {}
    for group in icecat.get("FeaturesGroups") or []:
        if not isinstance(group, dict):
            continue
        for feature in group.get("Features") or []:
            if not isinstance(feature, dict):
                continue
            name = _icecat_value((feature.get("Feature") or {}).get("Name"))
            value = _icecat_value(
                feature.get("PresentationValue"),
                feature.get("LocalValue"),
                feature.get("Value"),
            )
            if name and value and name not in specs:
                specs[name] = value
    return specs


def _icecat_gallery(icecat: dict) -> list:
    """Display-sized gallery images, largest-useful first, de-duplicated."""
    gallery: list[str] = []
    for entry in icecat.get("Gallery") or []:
        if not isinstance(entry, dict):
            continue
        url = _icecat_value(entry.get("Pic500x500"), entry.get("Pic"))
        if url and url not in gallery:
            gallery.append(url)
    return gallery


def _extract_product_data(data: MasterSKURequest) -> dict:
    icecat = _icecat(data.GTIN.strip(), data.Make.strip(), data.Model.strip(), data.locale)
    upc = _go_upc(data.GTIN.strip()) if not icecat else {}
    general = icecat.get("GeneralInfo") or {}
    upc_product = upc.get("product") or {}

    brand = general.get("Brand") or general.get("BrandName") or upc_product.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("Value") or brand.get("Name")
    name_info = general.get("ProductNameInfo") if isinstance(general.get("ProductNameInfo"), dict) else {}
    # Icecat separates the manufacturer part number ("58A6Q", shown as "Product code") from
    # the long marketing name ("58\" A6QTUK 4K Ultra HD Smart TV with Freely"). Only the part
    # number belongs in identifiers.model: it builds the DataforSEO search keyword and is the
    # string matched against merchant listing titles when the results come back. Using the
    # marketing name as the model makes every shopping enrichment miss.
    product_code = _icecat_value(
        general.get("BrandPartCode"),
        name_info.get("BrandPartCode"),
        general.get("ProductCode"),
        name_info.get("ProductIntName"),
    )
    # The locale drives the Icecat lookup language, and the resulting copy lands in this
    # SKU's locale-specific block — so pick the language-tagged variants over the
    # international ones. The part number above stays language-independent.
    language = str(data.locale or "")[:2]
    title_info = general.get("TitleInfo") if isinstance(general.get("TitleInfo"), dict) else {}
    product_name = _icecat_localised(
        language,
        name_info.get("ProductLocalName"),
        general.get("ProductName"),
    )
    title = _icecat_localised(
        language,
        title_info.get("BrandLocalTitle"),
        title_info.get("GeneratedLocalTitle"),
        general.get("Title"),
        name_info.get("ProductLocalName"),
        general.get("ProductName"),
        title_info.get("GeneratedIntTitle"),
    ) or _icecat_value(upc_product.get("name"))
    raw_category = general.get("Category") or {}
    category = raw_category.get("Name") if isinstance(raw_category, dict) else raw_category
    if isinstance(category, dict):
        category = category.get("Value") or category.get("Name")
    source_category = category or upc_product.get("category") or ""
    category_text = " ".join(
        str(value).strip()
        for value in (source_category, upc_product.get("name"))
        if value and str(value).strip()
    )
    category = _canonical_category(category_text, data.Category)

    icecat_image = icecat.get("Image") if isinstance(icecat.get("Image"), dict) else {}
    high_pic = _icecat_value(icecat_image.get("HighPic"))
    # HighPic is the print-resolution original (3072px / ~2.7 MB for this TV) and the card
    # renders it at 160px. Pic500x500 is the same shot at display size, so prefer it and
    # keep the original alongside for anything that needs to zoom.
    cover = general.get("CoverPicture")
    if isinstance(cover, dict):
        cover = cover.get("Pic") or cover.get("Value")
    image_url = (
        _icecat_value(cover, icecat_image.get("Pic500x500"), high_pic)
        or upc_product.get("imageUrl")
    )
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
        "model": str(data.Model or product_code or product_name or "").strip(),
        "gtins": gtins,
        "title": str(title or "").strip(),
        "category": str(category or "").strip(),
        "imageUrl": image_url,
        "highResImage": high_pic,
        "gallery": _icecat_gallery(icecat),
        "media": media,
        "description": _icecat_description(general),
        "features": _icecat_features(general, language),
        "specifications": _icecat_specifications(icecat),
        "icecatId": _icecat_value(general.get("IcecatId")),
        "releaseDate": _icecat_value(general.get("ReleaseDate")),
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
    if product.get("highResImage") and product["highResImage"] != product.get("imageUrl"):
        assets["highResImage"] = product["highResImage"]
    if product.get("gallery"):
        assets["gallery"] = list(product["gallery"])
    documents = []
    # Only the documents need base_url — they are served back through the masking proxy.
    # Images are absolute Icecat URLs, so they must survive a missing base_url.
    for item in (product.get("media") or []) if base_url else []:
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
        "assets": _assets(product, base_url),
        # Icecat already carries the copy the product card renders. Populating it here
        # means a SKU is presentable immediately, rather than only after the DataforSEO
        # round-trip — which may match nothing, or never run at all when add_pricing
        # is false.
        "description": product.get("description") or "",
        "features": list(product.get("features") or []),
        "specifications": dict(product.get("specifications") or {}),
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
        "provenance": {
            "createdFrom": "catalog-api",
            **({"icecatId": product["icecatId"]} if product.get("icecatId") else {}),
            **({"releaseDate": product["releaseDate"]} if product.get("releaseDate") else {}),
        },
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
