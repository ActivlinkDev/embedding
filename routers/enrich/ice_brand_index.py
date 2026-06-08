"""
ice_brand_index.py — Icecat batch lookup endpoint

POST /ice/batch-lookup
  Accepts a list of identifiers (GTINs and/or brand+productcode pairs) and fans
  them out concurrently to the Icecat single-product API.  Returns a result for
  every identifier — hit, miss, or error — so callers can see exactly what was
  found without looping manually.

Why not index-file based?
  The Icecat bulk index XML download (freexml.int.gz) requires a Full Icecat
  subscription.  Open Icecat accounts (e.g. plyford) only support individual
  product lookups, so this endpoint maximises throughput by running all lookups
  in parallel via asyncio/httpx.
"""

import asyncio
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(prefix="/ice", tags=["Enrichment"])

ICECAT_USERNAME = os.getenv("ICECAT_USER", "")
BASE_URL = "https://live.icecat.biz/api/"
MAX_CONCURRENT = 10          # stay polite to Icecat's servers
REQUEST_TIMEOUT = 15.0       # seconds per individual lookup


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class IcecatIdentifier(BaseModel):
    gtin: Optional[str] = Field(None, description="GTIN / EAN / UPC")
    brand: Optional[str] = Field(None, description="Brand name (required if no GTIN)")
    productcode: Optional[str] = Field(None, description="Manufacturer product code (required if no GTIN)")
    ref: Optional[str] = Field(None, description="Optional caller reference echoed back in the result")


class BatchLookupRequest(BaseModel):
    identifiers: list[IcecatIdentifier] = Field(..., min_items=1, max_items=200)
    lang: str = Field("en", description="2-letter language code")


# ---------------------------------------------------------------------------
# Core lookup helper
# ---------------------------------------------------------------------------

async def _lookup_one(
    client: httpx.AsyncClient,
    ident: IcecatIdentifier,
    lang: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Perform a single Icecat lookup and return a normalised result dict."""
    async with semaphore:
        base = f"{BASE_URL}?username={ICECAT_USERNAME}&lang={lang}"

        # Prefer GTIN; fall back to brand + productcode
        if ident.gtin:
            url = f"{base}&GTIN={ident.gtin}"
            tried = f"GTIN={ident.gtin}"
        elif ident.brand and ident.productcode:
            url = f"{base}&brand={ident.brand}&productcode={ident.productcode}"
            tried = f"brand={ident.brand}&productcode={ident.productcode}"
        else:
            return {
                "ref": ident.ref,
                "status": "error",
                "error": "Must supply gtin OR both brand and productcode",
                "data": None,
            }

        try:
            resp = await client.get(url, timeout=REQUEST_TIMEOUT)
        except httpx.RequestError as exc:
            return {
                "ref": ident.ref,
                "tried": tried,
                "status": "error",
                "error": str(exc),
                "data": None,
            }

        if resp.status_code == 200:
            return {
                "ref": ident.ref,
                "tried": tried,
                "status": "found",
                "error": None,
                "data": resp.json(),
            }

        return {
            "ref": ident.ref,
            "tried": tried,
            "status": "not_found",
            "error": f"HTTP {resp.status_code}",
            "data": None,
        }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/batch-lookup", dependencies=[Depends(verify_token)])
async def batch_lookup(body: BatchLookupRequest):
    """
    Look up multiple products in Icecat concurrently.

    Supply up to 200 identifiers per request — each can use a GTIN or a
    brand + productcode pair.  An optional `ref` field is echoed back so you
    can correlate results with your own records.

    Returns one result object per identifier with:
    - `status`: "found" | "not_found" | "error"
    - `data`: full Icecat product sheet (when found)
    - `error`: reason string (when not found or errored)
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with httpx.AsyncClient() as client:
        tasks = [
            _lookup_one(client, ident, body.lang, semaphore)
            for ident in body.identifiers
        ]
        results = await asyncio.gather(*tasks)

    found = sum(1 for r in results if r["status"] == "found")
    not_found = sum(1 for r in results if r["status"] == "not_found")
    errors = sum(1 for r in results if r["status"] == "error")

    return {
        "summary": {
            "total": len(results),
            "found": found,
            "not_found": not_found,
            "errors": errors,
        },
        "results": results,
    }
