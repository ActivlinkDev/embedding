import gzip
import os
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query

from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(prefix="/ice", tags=["Enrichment"])

ICECAT_USERNAME = os.getenv("ICECAT_USER", "")
ICECAT_PASSWORD = os.getenv("ICECAT_PASS", "")

# Icecat index: free tier (Open Icecat). For Full Icecat use xml_full.gz.
INDEX_URL = "https://icecat.us/export/freexml.int.gz"

# Simple in-process cache so repeated calls don't re-download the ~50MB index.
_cache: dict = {"data": None, "fetched_at": 0}
CACHE_TTL = 3600  # seconds — Icecat publishes a new index daily, 1h is fine


def _fetch_index() -> list[dict]:
    """Download and parse the Icecat product index, returning a flat list of product dicts."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["fetched_at"] < CACHE_TTL:
        return _cache["data"]

    auth = (ICECAT_USERNAME, ICECAT_PASSWORD) if ICECAT_PASSWORD else (ICECAT_USERNAME, ICECAT_USERNAME)
    resp = requests.get(INDEX_URL, auth=auth, timeout=120, stream=True)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Icecat index fetch failed: HTTP {resp.status_code}",
        )

    raw = gzip.decompress(resp.content)
    root = ET.fromstring(raw)

    products = []
    for file_el in root.iter("file"):
        supplier = (file_el.findtext("Supplier") or "").strip()
        prod_id = file_el.get("Product_id", "")
        prod_name = file_el.get("Name", "")
        prod_code = file_el.get("Prod_id", "")
        ean = file_el.get("EAN_UPCS", "")
        category = file_el.findtext("Category") or ""
        updated = file_el.get("Updated", "")

        products.append(
            {
                "product_id": prod_id,
                "name": prod_name,
                "product_code": prod_code,
                "ean": ean,
                "supplier": supplier,
                "category": category,
                "updated": updated,
            }
        )

    _cache["data"] = products
    _cache["fetched_at"] = now
    return products


@router.get("/brand-index", dependencies=[Depends(verify_token)])
def brand_index(
    brand: str = Query(..., description="Brand / manufacturer name (case-insensitive, partial match)"),
    category: Optional[str] = Query(None, description="Optional category filter (case-insensitive, partial match)"),
    limit: int = Query(500, ge=1, le=5000, description="Max products to return"),
):
    """
    Return all Icecat products for a brand from the full index XML.
    Much faster than one-by-one SKU lookups for bulk ingestion.
    The index is cached in-process for 1 hour.
    """
    all_products = _fetch_index()

    brand_lower = brand.lower()
    cat_lower = category.lower() if category else None

    matches = [
        p for p in all_products
        if brand_lower in p["supplier"].lower()
        and (cat_lower is None or cat_lower in p["category"].lower())
    ]

    return {
        "brand": brand,
        "category_filter": category,
        "total_matched": len(matches),
        "returned": min(len(matches), limit),
        "products": matches[:limit],
    }
