"""Import EPREL (EU energy label registry) records into a client's CustomSKU catalog.

POST /eprel/import imports a single registered model; POST /eprel/import/brand
walks every scraped record for a brand as a background job. Both reuse the
standard create flow (create_custom_sku_service), so imported SKUs are
indistinguishable from manually created ones — same MasterSKU resolution,
enrichment, and locale handling.

Records are read from the EPREL_Products collection populated by
routers/eprel_scrape.py. Product groups are translated to Activlink categories
through the EPREL_Category_Map collection (see utils/eprel_mapping.py); a group
with no usable mapping is skipped rather than imported as "Unknown".
"""

import asyncio
import logging
import os
import re
import time
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo import MongoClient

from utils import eprel
from utils.dependencies import verify_token
from utils.eprel_mapping import (
    REASON_OK,
    eprel_to_custom_sku_request,
    make_from,
    resolve_category,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/eprel",
    tags=["Enrichment"],
    dependencies=[Depends(verify_token)],
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("MONGO_DB", "Activlink")]
products_collection = db["EPREL_Products"]
jobs_collection = db["EPREL_ImportJobs"]
category_map_collection = db["EPREL_Category_Map"]
category_collection = db["Category"]
locale_collection = db["Locale_Params"]
client_collection = db["ClientKey"]

# Outcome buckets recorded per (record, locale).
CREATED = "created"
LOCALE_ADDED = "locale_added"
ALREADY_EXISTS = "already_exists"
FAILED = "failed"


class ImportRequest(BaseModel):
    clientKey: str
    locales: list[str] = Field(..., min_length=1, description="Locales to import into, e.g. ['en_GB','fr_FR']")
    eprelRegistrationNumber: str
    add_pricing: bool = True


class BrandImportRequest(BaseModel):
    clientKey: str
    locales: list[str] = Field(..., min_length=1, description="Locales to import into")
    brand: str = Field(..., min_length=2)
    groups: list[str] | None = Field(None, description="EPREL product group url codes (default: all)")
    add_pricing: bool = Field(False, description="Off by default: enrichment is charged per SKU per locale")
    delay: float = Field(0.2, ge=0, le=5, description="Seconds between records")


class CategoryMapEntry(BaseModel):
    name: str = Field(..., min_length=2, description="Readable appliance name used for category matching")
    category: str | None = Field(None, description="Pinned canonical category; skips embedding lookup")
    enabled: bool = True


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_locales(locales: list[str]) -> None:
    """Reject unknown locales before any work starts."""
    unknown = [l for l in locales if not locale_collection.find_one({"locale": l})]
    if unknown:
        raise HTTPException(status_code=404, detail=f"Locale(s) not found: {', '.join(unknown)}")


def _validate_client(client_key: str) -> None:
    if not client_collection.find_one({"ClientKey": client_key}):
        raise HTTPException(status_code=404, detail=f"ClientKey {client_key} not found.")


def _run_background_tasks_inline(background_tasks: BackgroundTasks) -> None:
    """Execute tasks the create flow queued, since no HTTP response will flush them."""
    for task in background_tasks.tasks:
        try:
            if asyncio.iscoroutinefunction(task.func):
                asyncio.run(task.func(*task.args, **task.kwargs))
            else:
                task.func(*task.args, **task.kwargs)
        except Exception:
            logger.exception("[eprel_import] queued task %s failed", getattr(task.func, "__name__", task.func))


def _classify(result: dict) -> tuple[str, str | None]:
    """Map a create_custom_sku_service return value onto an outcome bucket."""
    message = result.get("message")
    if not message:
        return CREATED, None
    if message.startswith("SKU exists already"):
        return ALREADY_EXISTS, None
    if message.startswith("Locale added"):
        return LOCALE_ADDED, None
    return FAILED, message


def _import_one(doc: dict, client_key: str, locales: list[str], category: str,
                add_pricing: bool) -> list[dict]:
    """Import a single EPREL record across every requested locale."""
    from routers.sku.create_custom_sku import create_custom_sku_service

    results = []
    for locale in locales:
        try:
            request_data = eprel_to_custom_sku_request(doc, client_key, locale, category, add_pricing)
            background_tasks = BackgroundTasks()
            result = create_custom_sku_service(request_data, background_tasks)
            _run_background_tasks_inline(background_tasks)
            outcome, reason = _classify(result)
            entry = {"locale": locale, "outcome": outcome}
            if reason:
                entry["reason"] = reason
            else:
                entry["customsku"] = result.get("customsku") or result.get("existing") or result
            results.append(entry)
        except HTTPException as exc:
            results.append({"locale": locale, "outcome": FAILED, "reason": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001 - one locale must not sink the record
            logger.exception("[eprel_import] locale %s failed for %s", locale, doc.get("modelIdentifier"))
            results.append({"locale": locale, "outcome": FAILED, "reason": str(exc)})
    return results


@router.post("/import", status_code=200)
def import_record(payload: ImportRequest):
    """Import one EPREL model into a client's catalog, across one or more locales.

    Importing into several locales produces a single CustomSKU carrying a locale
    block for each, not one document per locale. If the registration number has
    not been scraped yet it is fetched from EPREL and stored on the way through.
    """
    _validate_locales(payload.locales)
    _validate_client(payload.clientKey)

    ern = payload.eprelRegistrationNumber.strip()
    doc = products_collection.find_one({"eprelRegistrationNumber": ern})
    if not doc:
        live = eprel.fetch_product(eprel.make_session(), ern)
        if not live:
            raise HTTPException(status_code=404, detail=f"EPREL registration number {ern} not found.")
        group = live.get("productGroup") or ""
        eprel.store_in_mongo(products_collection, [live], group, make_from(live))
        doc = products_collection.find_one({"eprelRegistrationNumber": ern}) or live

    product_group = doc.get("productGroup") or ""
    category, reason = resolve_category(category_map_collection, product_group, {})
    if not category:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Cannot import {product_group}: {reason}",
                "productGroup": product_group,
                "hint": "Configure it via PUT /eprel/category-map/{product_group}",
            },
        )

    results = _import_one(doc, payload.clientKey, payload.locales, category, payload.add_pricing)
    return {
        "eprelRegistrationNumber": ern,
        "productGroup": product_group,
        "category": category,
        "model": doc.get("modelIdentifier"),
        "results": results,
    }


def _run_brand_import(job_id: str, payload: BrandImportRequest, query: dict) -> None:
    jobs_collection.update_one({"jobId": job_id}, {"$set": {"status": "running", "startedAt": _now()}})

    totals = {"records": 0, "processed": 0, "skipped_no_category": 0, "failed": 0}
    per_locale = {locale: {CREATED: 0, LOCALE_ADDED: 0, ALREADY_EXISTS: 0, FAILED: 0}
                  for locale in payload.locales}
    errors: list[dict] = []
    category_cache: dict = {}

    def flush() -> None:
        update = {"$set": {"totals": totals, "perLocale": per_locale, "updatedAt": _now()}}
        if errors:
            update["$set"]["errors"] = errors[:100]
        jobs_collection.update_one({"jobId": job_id}, update)

    try:
        totals["records"] = products_collection.count_documents(query)
        for index, doc in enumerate(products_collection.find(query), start=1):
            model = doc.get("modelIdentifier")
            try:
                category, reason = resolve_category(
                    category_map_collection, doc.get("productGroup") or "", category_cache
                )
                if not category:
                    totals["skipped_no_category"] += 1
                    if len(errors) < 100:
                        errors.append({"ern": doc.get("eprelRegistrationNumber"), "model": model,
                                       "locale": None, "reason": reason})
                    continue

                for entry in _import_one(doc, payload.clientKey, payload.locales,
                                         category, payload.add_pricing):
                    bucket = per_locale.setdefault(
                        entry["locale"], {CREATED: 0, LOCALE_ADDED: 0, ALREADY_EXISTS: 0, FAILED: 0}
                    )
                    bucket[entry["outcome"]] += 1
                    if entry["outcome"] == FAILED and len(errors) < 100:
                        errors.append({"ern": doc.get("eprelRegistrationNumber"), "model": model,
                                       "locale": entry["locale"], "reason": entry.get("reason")})
                totals["processed"] += 1
            except Exception as exc:  # noqa: BLE001 - one record must not sink the job
                logger.exception("[eprel_import] record %s failed", model)
                totals["failed"] += 1
                if len(errors) < 100:
                    errors.append({"ern": doc.get("eprelRegistrationNumber"), "model": model,
                                   "locale": None, "reason": str(exc)})

            if index % 10 == 0:
                flush()
            if payload.delay:
                time.sleep(payload.delay)

        flush()
        jobs_collection.update_one(
            {"jobId": job_id},
            {"$set": {"status": "completed", "finishedAt": _now(),
                      "totals": totals, "perLocale": per_locale}},
        )
    except Exception as exc:  # noqa: BLE001 - the job must record its own failure
        logger.exception("[eprel_import] brand import %s failed", job_id)
        jobs_collection.update_one(
            {"jobId": job_id},
            {"$set": {"status": "failed", "finishedAt": _now(), "error": str(exc),
                      "totals": totals, "perLocale": per_locale}},
        )


@router.post("/import/brand", status_code=202)
def import_brand(payload: BrandImportRequest, background_tasks: BackgroundTasks):
    """Import every scraped EPREL record for a brand into a client's catalog.

    Returns a job id immediately: a large brand across several locales runs for
    many minutes. Poll GET /eprel/import/jobs/{job_id} for progress.
    """
    _validate_locales(payload.locales)
    _validate_client(payload.clientKey)

    brand = re.escape(payload.brand.strip())
    query: dict = {
        "$or": [
            {"supplierOrTrademark": {"$regex": f"^{brand}$", "$options": "i"}},
            {"manufacturerQuery": {"$regex": f"^{brand}$", "$options": "i"}},
        ]
    }
    if payload.groups:
        query["productGroup"] = {"$in": payload.groups}

    record_count = products_collection.count_documents(query)
    if record_count == 0:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"No scraped EPREL records for brand '{payload.brand}'.",
                "hint": "Run POST /eprel/scrape for this brand first.",
            },
        )

    job_id = uuid.uuid4().hex
    jobs_collection.insert_one({
        "jobId": job_id,
        "status": "queued",
        "clientKey": payload.clientKey,
        "brand": payload.brand,
        "locales": payload.locales,
        "groups": payload.groups,
        "addPricing": payload.add_pricing,
        "totals": {"records": record_count, "processed": 0, "skipped_no_category": 0, "failed": 0},
        "perLocale": {},
        "errors": [],
        "createdAt": _now(),
    })
    background_tasks.add_task(_run_brand_import, job_id, payload, query)
    return {
        "job_id": job_id,
        "status": "queued",
        "brand": payload.brand,
        "locales": payload.locales,
        "records": record_count,
        "status_url": f"/eprel/import/jobs/{job_id}",
    }


@router.get("/import/jobs/{job_id}")
def get_import_job(job_id: str):
    """Progress and per-locale totals for an import job."""
    job = jobs_collection.find_one({"jobId": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/category-map")
def list_category_map():
    """List EPREL product group to category mappings, and which groups are importable."""
    mappings = list(category_map_collection.find({}, {"_id": 0}).sort("productGroup", 1))
    unresolved = [m["productGroup"] for m in mappings
                  if m.get("enabled", True) and not (m.get("category") or "").strip()]
    return {"mappings": mappings, "count": len(mappings), "unpinned": unresolved}


@router.put("/category-map/{product_group}")
def upsert_category_map(product_group: str, payload: CategoryMapEntry):
    """Create or correct the category mapping for one EPREL product group."""
    if payload.category:
        if not category_collection.find_one({"category": payload.category}):
            raise HTTPException(
                status_code=422,
                detail=f"Category '{payload.category}' does not exist in the Category collection.",
            )

    category_map_collection.update_one(
        {"productGroup": product_group},
        {"$set": {
            "productGroup": product_group,
            "name": payload.name,
            "category": payload.category,
            "enabled": payload.enabled,
        }},
        upsert=True,
    )
    return category_map_collection.find_one({"productGroup": product_group}, {"_id": 0})
