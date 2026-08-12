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

from utils.api_docs import error, json_response, secured
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
    """Which brand to scrape from EPREL, and how much of it."""

    brand: str = Field(
        ...,
        min_length=2,
        description=(
            "**Mandatory, at least 2 characters.** Matched against EPREL's "
            "`supplierOrTrademark` field, so use the name as it appears on the energy label "
            "(`Samsung`, `BSH Hausgeräte`) rather than an internal client name."
        ),
        examples=["Samsung"],
    )
    groups: list[str] | None = Field(
        None,
        description=(
            "EPREL product group url codes to scrape. **Omit to scrape every group**, which is "
            "the slow path. Codes come from `GET /eprel/product-groups`; an unrecognised one is "
            "rejected with `422` listing the valid codes."
        ),
        examples=[["dishwashers2019", "washingmachines2019"]],
    )
    delay: float = Field(
        0.5,
        ge=0.1,
        le=5,
        description=(
            "Seconds to wait between EPREL requests, between `0.1` and `5`. Raise it if EPREL "
            "starts rate-limiting; lowering it makes a large scrape faster but ruder."
        ),
        examples=[0.5],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "brand": "Samsung",
                "groups": ["dishwashers2019", "washingmachines2019"],
                "delay": 0.5,
            }
        }
    }


class ScrapeAccepted(BaseModel):
    """Acknowledgement that a scrape job was queued. **No products have been fetched yet.**"""

    job_id: str = Field(..., description="Identifier to poll the job with.", examples=["3f9a1c2d4e5b6a7c8d9e0f1a2b3c4d5e"])
    status: str = Field(..., description="Always `queued` here — the work starts after the response is sent.", examples=["queued"])
    brand: str = Field(..., description="The brand being scraped, echoed back.", examples=["Samsung"])
    groups: list[str] = Field(
        ...,
        description="The product groups this job will walk — every group when none were requested.",
        examples=[["dishwashers2019", "washingmachines2019"]],
    )
    status_url: str = Field(
        ...,
        description="Path to poll for progress.",
        examples=["/eprel/scrape/jobs/3f9a1c2d4e5b6a7c8d9e0f1a2b3c4d5e"],
    )


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


@router.post(
    "/scrape",
    response_model=ScrapeAccepted,
    status_code=202,
    summary="Start a background EPREL scrape for a brand",
    response_description="The job id and the URL to poll. Nothing has been scraped yet.",
    responses=secured({
        202: json_response(
            "The job was queued. **Accepted, not done** — poll `status_url` for progress.",
            {
                "job_id": "3f9a1c2d4e5b6a7c8d9e0f1a2b3c4d5e",
                "status": "queued",
                "brand": "Samsung",
                "groups": ["dishwashers2019", "washingmachines2019"],
                "status_url": "/eprel/scrape/jobs/3f9a1c2d4e5b6a7c8d9e0f1a2b3c4d5e",
            },
        ),
        422: error(
            "One or more requested `groups` are not EPREL product group codes. The detail "
            "carries the full list of valid codes.",
            {
                "message": "Unknown product group(s): dishwashers",
                "validGroups": ["airconditioners2019", "dishwashers2019", "washingmachines2019"],
            },
        ),
        502: error("EPREL could not be reached to list the product groups.", "EPREL unreachable: ..."),
    }),
)
def start_scrape(payload: ScrapeRequest, background_tasks: BackgroundTasks):
    """
    Scrape every model a brand has registered in **EPREL** — the EU energy label registry — into
    the `EPREL_Products` collection.

    **This is asynchronous.** The response is `202 Accepted` with a `job_id`: the scrape itself
    starts after the response is sent and a full brand takes **several minutes**, since it pages
    through every product group and pauses `delay` seconds between requests. Poll
    `GET /eprel/scrape/jobs/{job_id}` for progress; there is no webhook.

    Only `brand` is mandatory. Omit `groups` to walk every product group, or narrow to the codes
    you need — a bad code fails fast with `422` before any work starts, and the error lists the
    valid codes.

    **Safe to re-run.** Products are upserted on `eprelRegistrationNumber`, so a repeat scrape
    updates rows in place rather than duplicating them; the job totals split this into `inserted`
    and `updated`. Nothing is ever deleted, so a model EPREL has withdrawn stays behind.

    Two things worth knowing before relying on it:

    - **Failures after acceptance are reported in the job, not here.** EPREL going down mid-scrape
      leaves the job `failed` with an `error`, while this call already returned `202`.
    - **The job lives in this process.** A restart or redeploy mid-scrape abandons it, and the job
      document is left showing `running` forever. Treat a `running` job that has stopped advancing
      as dead and start a new one.
    """
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


@router.get(
    "/scrape/jobs/{job_id}",
    summary="Poll the progress of a scrape job",
    response_description="The job document: status, per-group results and running totals.",
    responses=secured({
        200: json_response(
            "The job as it currently stands. **Check `status`** — a failed scrape is reported "
            "here with `200`, not with an error status.",
            {
                "jobId": "3f9a1c2d4e5b6a7c8d9e0f1a2b3c4d5e",
                "status": "completed",
                "brand": "Samsung",
                "requestedGroups": ["dishwashers2019", "washingmachines2019"],
                "groups": [
                    {"productGroup": "dishwashers2019", "name": "Dishwashers", "models": 142, "inserted": 140, "updated": 2},
                    {"productGroup": "washingmachines2019", "name": "Washing machines", "models": 208, "inserted": 201, "updated": 7},
                ],
                "totals": {"models": 350, "inserted": 341, "updated": 9},
                "createdAt": "2026-08-08T22:14:52Z",
                "startedAt": "2026-08-08T22:14:53Z",
                "finishedAt": "2026-08-08T22:19:31Z",
            },
        ),
        404: error("No job with this id.", "Job not found"),
    }),
)
def get_scrape_job(job_id: str):
    """
    Poll a scrape started by `POST /eprel/scrape`.

    Path parameter `job_id` is mandatory. The job document is returned as stored and updates
    **after each product group**, so `groups` and `totals` grow as the scrape proceeds — that is
    how you watch a long run rather than waiting on it.

    **`status` is the outcome, not the HTTP code.** All four states come back as `200`:

    | `status` | Meaning |
    | --- | --- |
    | `queued` | Accepted, not started |
    | `running` | In progress; `groups` and `totals` are partial |
    | `completed` | Every requested group was walked |
    | `failed` | Aborted; `error` holds the reason and `totals` what was stored before it |

    A `failed` job keeps whatever it had already written — the scrape is not transactional — so
    re-running is the fix, and the upsert makes that harmless.

    In `totals`, `models` counts what EPREL returned while `inserted` + `updated` counts what was
    stored; they differ when EPREL returns records with no `eprelRegistrationNumber`, which are
    skipped since there is no key to upsert them on.

    A job stuck in `running` with totals that stop moving has almost certainly been lost to a
    restart — jobs are held in process memory and are not resumed.
    """
    job = jobs_collection.find_one({"jobId": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get(
    "/product-groups",
    summary="List the EPREL product groups available to scrape",
    response_description="EPREL's product group list, passed through unchanged.",
    responses=secured({
        200: json_response(
            "The live list from EPREL. Use `url_code` values in the `groups` field of "
            "`POST /eprel/scrape`.",
            [
                {"url_code": "dishwashers2019", "name": "Dishwashers"},
                {"url_code": "washingmachines2019", "name": "Washing machines"},
                {"url_code": "airconditioners2019", "name": "Air conditioners"},
            ],
        ),
        502: error("EPREL could not be reached.", "EPREL unreachable: ..."),
    }),
)
def list_product_groups():
    """
    List the EPREL product groups (appliance types) that can be scraped.

    Takes no parameters. Call this first to pick `groups` for `POST /eprel/scrape` — it is the
    `url_code` of each entry that the scrape accepts, not the display `name`.

    The list is fetched **live from EPREL** on every call and forwarded unchanged, so its shape
    is EPREL's contract rather than this API's, and it reflects any group the EU adds or retires.
    Nothing is cached or stored; if EPREL is down this returns `502` and a scrape would fail the
    same way.
    """
    try:
        return eprel.get_product_groups(eprel.make_session())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"EPREL unreachable: {exc}")
