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

from utils.api_docs import json_response, secured
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
    """One product to look up. Every field is optional individually, but each entry needs
    **either a `gtin` or both `brand` and `productcode`** — an entry with neither cannot be
    looked up and comes back with `status: "error"`."""

    gtin: Optional[str] = Field(None, description="GTIN / EAN / UPC. Tried first.", examples=["5011773057240"])
    brand: Optional[str] = Field(None, description="Brand name. Required with `productcode` when there is no GTIN.", examples=["Bosch"])
    productcode: Optional[str] = Field(None, description="Manufacturer product code. Required with `brand`.", examples=["SMS6ZCI00G"])
    ref: Optional[str] = Field(
        None,
        description="Your own reference, echoed back on the matching result — the reliable way to correlate results with your records.",
        examples=["row-17"],
    )


class BatchLookupRequest(BaseModel):
    """Up to 200 products to look up in one call."""

    identifiers: list[IcecatIdentifier] = Field(
        ...,
        min_items=1,
        max_items=200,
        description="**Mandatory. Between 1 and 200 entries.** Requests outside that range are rejected with `422`.",
    )
    lang: str = Field("en", description="Two-letter language code applied to every lookup in the batch.", examples=["en"])

    model_config = {
        "json_schema_extra": {
            "example": {
                "lang": "en",
                "identifiers": [
                    {"gtin": "5011773057240", "ref": "row-1"},
                    {"brand": "Bosch", "productcode": "SMS4HCI40G", "ref": "row-2"},
                ],
            }
        }
    }


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

@router.post(
    "/batch-lookup",
    dependencies=[Depends(verify_token)],
    summary="Look up many products in Icecat at once",
    response_description="A count summary plus one result per identifier, in the order submitted.",
    responses=secured({
        200: json_response(
            "The batch ran. Per-identifier failures appear inside `results`; the call itself "
            "still succeeds.",
            {
                "summary": {"total": 2, "found": 1, "not_found": 1, "errors": 0},
                "results": [
                    {
                        "ref": "row-1",
                        "tried": ["gtin"],
                        "status": "found",
                        "data": {"GeneralInfo": {"Title": "Bosch Series 6 Dishwasher", "Brand": "Bosch"}},
                    },
                    {
                        "ref": "row-2",
                        "tried": ["gtin", "brand+productcode"],
                        "status": "not_found",
                        "error": "HTTP 404",
                        "data": None,
                    },
                ],
            },
        ),
    }),
)
async def batch_lookup(body: BatchLookupRequest):
    """
    Look up many products in Icecat in one call — the bulk form of `GET /ice/lookup`, for
    catalogue imports and coverage checks.

    Between **1 and 200** identifiers per request; each needs either a `gtin` or a
    `brand`+`productcode` pair. Lookups run concurrently (10 at a time, 15 second timeout each)
    and results come back **in the order submitted**.

    **A failed lookup does not fail the request.** Every identifier gets a result object:

    | Field | Meaning |
    | --- | --- |
    | `status` | `found`, `not_found`, or `error` |
    | `data` | The Icecat product sheet when found, otherwise `null` |
    | `error` | Why it failed, e.g. `HTTP 404` |
    | `tried` | Which strategies were attempted, in order |
    | `ref` | Your reference, echoed back |

    `summary` gives the counts at a glance. Set `ref` on each identifier — it is the reliable way
    to line results up with your own rows. Nothing is stored.
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
