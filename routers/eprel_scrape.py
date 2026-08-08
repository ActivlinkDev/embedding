"""Scrape EPREL (EU energy label registry) data for a brand into MongoDB.

POST /eprel/scrape kicks off a background job that walks every EPREL product
group (appliance type) for the given brand and upserts each registered model
into the EPREL_Products collection, keyed on eprelRegistrationNumber so
re-runs update in place. A full scrape of a large brand takes several
minutes, so the endpoint returns a job id immediately; poll
GET /eprel/scrape/jobs/{job_id} for progress and totals.
"""

import os
import time
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo import MongoClient

from utils.dependencies import verify_token
from utils import eprel

router = APIRouter(
    prefix="/eprel",
    tags=["Enrichment"],
    dependencies=[Depends(verify_token)],
)

client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("MONGO_DB", "Activlink")]
products_collection = db["EPREL_Products"]
jobs_collection = db["EPREL_ScrapeJobs"]


class ScrapeRequest(BaseModel):
    brand: str = Field(..., min_length=2, description="Supplier or trademark name, e.g. 'Samsung'")
    groups: list[str] | None = Field(
        None,
        description="EPREL product group url codes to scrape (default: all groups)",
    )
    delay: float = Field(0.5, ge=0.1, le=5, description="Seconds between EPREL requests")


class ScrapeAccepted(BaseModel):
    job_id: str
    status: str
    brand: str
    groups: list[str]
    status_url: str


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run_scrape(job_id: str, brand: str, groups: list[dict], delay: float) -> None:
    jobs_collection.update_one(
        {"jobId": job_id},
        {"$set": {"status": "running", "startedAt": _now()}},
    )
    session = eprel.make_session()
    totals = {"models": 0, "inserted": 0, "updated": 0}
    try:
        eprel.ensure_indexes(products_collection)
        for group in groups:
            code = group["url_code"]
            products = eprel.scrape_group(session, code, brand, delay)
            inserted, updated = eprel.store_in_mongo(products_collection, products, code, brand)
            totals["models"] += len(products)
            totals["inserted"] += inserted
            totals["updated"] += updated
            jobs_collection.update_one(
                {"jobId": job_id},
                {
                    "$set": {"totals": totals, "updatedAt": _now()},
                    "$push": {
                        "groups": {
                            "productGroup": code,
                            "name": group.get("name"),
                            "models": len(products),
                            "inserted": inserted,
                            "updated": updated,
                        }
                    },
                },
            )
            time.sleep(delay)
        jobs_collection.update_one(
            {"jobId": job_id},
            {"$set": {"status": "completed", "finishedAt": _now(), "totals": totals}},
        )
    except Exception as exc:  # noqa: BLE001 - job must record any failure
        jobs_collection.update_one(
            {"jobId": job_id},
            {"$set": {"status": "failed", "finishedAt": _now(), "error": str(exc), "totals": totals}},
        )


@router.post("/scrape", response_model=ScrapeAccepted, status_code=202)
def start_scrape(payload: ScrapeRequest, background_tasks: BackgroundTasks):
    """Start a background scrape of all (or selected) EPREL product groups for a brand."""
    try:
        all_groups = eprel.get_product_groups(eprel.make_session())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"EPREL unreachable: {exc}")

    known = {g["url_code"]: g for g in all_groups}
    if payload.groups:
        unknown = [g for g in payload.groups if g not in known]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": f"Unknown product group(s): {', '.join(unknown)}",
                    "validGroups": sorted(known),
                },
            )
        groups = [known[g] for g in payload.groups]
    else:
        groups = all_groups

    job_id = uuid.uuid4().hex
    jobs_collection.insert_one(
        {
            "jobId": job_id,
            "status": "queued",
            "brand": payload.brand,
            "requestedGroups": [g["url_code"] for g in groups],
            "groups": [],
            "totals": {"models": 0, "inserted": 0, "updated": 0},
            "createdAt": _now(),
        }
    )
    background_tasks.add_task(_run_scrape, job_id, payload.brand, groups, payload.delay)
    return ScrapeAccepted(
        job_id=job_id,
        status="queued",
        brand=payload.brand,
        groups=[g["url_code"] for g in groups],
        status_url=f"/eprel/scrape/jobs/{job_id}",
    )


@router.get("/scrape/jobs/{job_id}")
def get_scrape_job(job_id: str):
    """Progress and totals for a scrape job (statuses: queued, running, completed, failed)."""
    job = jobs_collection.find_one({"jobId": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/product-groups")
def list_product_groups():
    """List EPREL product groups (appliance types) usable in the 'groups' filter."""
    try:
        return eprel.get_product_groups(eprel.make_session())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"EPREL unreachable: {exc}")
