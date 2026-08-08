"""Admin repair for CustomSKUs whose MSRP was never filled in.

CustomSKUs created before the MasterSKU price arrived were persisted with a blank
MSRP, which also left their widget quote cache cold. Going forward the DataforSEO
postback fixes this as the price lands (see ``propagate_master_price``), but SKUs
that missed that postback need a manual nudge — that's what this endpoint is for.

It reads the price already stored on the MasterSKU locale, copies it onto any
blank CustomSKU MSRP for the client, and re-warms the widget quote cache for the
SKUs it changed. SKUs whose MasterSKU still has no price are reported as
``unpriced`` and left alone.
"""

import os
from typing import Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo import MongoClient

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from routers.sku.propagate_master_price import (
    is_blank,
    master_locale_price,
    propagate_master_price,
)

load_dotenv()

router = APIRouter(prefix="/sku", tags=["Catalog"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
customsku_collection = db["CustomSKU"]
clientkey_collection = db["ClientKey"]


class BackfillMSRPRequest(BaseModel):
    """Which CustomSKUs to repair. Only `ClientKey` is mandatory — the optional fields narrow
    an otherwise client-wide sweep."""

    ClientKey: str = Field(
        ...,
        description="**Mandatory.** Tenant whose CustomSKUs are repaired. Unknown keys return `404`.",
        examples=["acme_uk_live"],
    )
    id: Optional[str] = Field(
        None,
        description=(
            "Single CustomSKU id to repair; omit to sweep the whole client. When set, sibling "
            "SKUs sharing the same MasterSKU and locale are **not** touched."
        ),
        examples=["681aa2f1c4b21d0f8c9e0012"],
    )
    Locale: Optional[str] = Field(
        None,
        description="Restrict the repair to one locale; omit to cover every locale on the matched SKUs.",
        examples=["en_GB"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {"ClientKey": "acme_uk_live", "id": None, "Locale": "en_GB"}
        }
    }


@router.post(
    "/backfill_msrp",
    summary="Backfill missing CustomSKU prices from their MasterSKU",
    response_description="How many SKUs were repaired, which ones, and which are still unpriced.",
    responses=secured({
        200: json_response(
            "The sweep ran. `backfilled: 0` with a populated `unpriced` means the MasterSKUs "
            "have no price yet — nothing was wrong with the request.",
            {
                "status": "ok",
                "backfilled": 2,
                "customskus": [
                    {"customSkuId": "681aa2f1c4b21d0f8c9e0012", "locale": "en_GB"},
                    {"customSkuId": "681aa2f1c4b21d0f8c9e0013", "locale": "en_GB"},
                ],
                "unpriced": [{"masterSkuId": "681aa2f1c4b21d0f8c9e0044", "locale": "fr_FR"}],
            },
        ),
        400: error("`id` is not a valid 24-character ObjectId.", "Invalid CustomSKU id"),
        404: error("No client is registered under this ClientKey.", "ClientKey acme_uk_live not found."),
    }),
)
def backfill_msrp(
    data: BackfillMSRPRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_token),
):
    """
    **Operational repair endpoint.** Copy prices from MasterSKUs onto CustomSKUs whose `MSRP` was
    left blank, and re-warm the widget quote cache for the ones it fixes.

    This exists for SKUs created *before* their MasterSKU price arrived: they were stored with no
    MSRP, which also left their widget quotes cold. Prices that land now are propagated
    automatically by the DataforSEO postback, so this is only needed for SKUs that missed it.

    `ClientKey` is mandatory and scopes the sweep. Narrow it with `id` (one CustomSKU) or
    `Locale` (one market); omit both to sweep the client's whole catalogue. When `id` is given,
    sibling SKUs sharing the same MasterSKU and locale are deliberately left alone.

    **Only blank MSRPs are touched** — an existing price is never overwritten, so the call is
    safe to repeat. A SKU whose MasterSKU also has no price is reported under `unpriced` and left
    unchanged; that is not an error, it means enrichment has not landed yet. Re-run once it has.

    `backfilled` counts SKU/locale pairs repaired. Cache warming happens in the background after
    the response, so quotes may take a moment to reflect the new price.
    """
    client_doc = clientkey_collection.find_one({"ClientKey": data.ClientKey})
    if not client_doc or "Client_ID" not in client_doc:
        raise HTTPException(status_code=404, detail=f"ClientKey {data.ClientKey} not found.")
    client_id = client_doc["Client_ID"]

    query = {"Client": client_id}
    if data.id:
        try:
            query["_id"] = ObjectId(data.id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid CustomSKU id")

    # Collect the (MasterSKU, locale) pairs that still need a price. Several
    # CustomSKUs can share a MasterSKU, so dedupe before hitting the master.
    pairs = set()
    unpriced = []
    for doc in customsku_collection.find(
        query, {"MasterSKU": 1, "Locale_Specific_Data.locale": 1, "Locale_Specific_Data.MSRP": 1}
    ):
        master_sku_id = doc.get("MasterSKU")
        if not master_sku_id:
            continue
        for entry in doc.get("Locale_Specific_Data") or []:
            if not isinstance(entry, dict):
                continue
            locale = entry.get("locale")
            if not locale or (data.Locale and locale != data.Locale):
                continue
            if is_blank(entry.get("MSRP")):
                pairs.add((str(master_sku_id), locale))

    warm_targets = []
    for master_sku_id, locale in sorted(pairs):
        price, currency = master_locale_price(master_sku_id, locale)
        if is_blank(price):
            # The MasterSKU has no price either — enrichment hasn't landed yet.
            unpriced.append({"masterSkuId": master_sku_id, "locale": locale})
            continue
        warm_targets.extend(
            propagate_master_price(
                master_sku_id, locale, price, currency,
                client_id=client_id,
                # Sibling SKUs can share this master and locale; when the caller
                # named one CustomSKU, repair only that one.
                custom_sku_id=query.get("_id"),
            )
        )

    if warm_targets:
        from routers.widget_quote import warm_widget_cache

        for warm_client_key, custom_sku_id, locale in warm_targets:
            background_tasks.add_task(warm_widget_cache, warm_client_key, custom_sku_id, locale)

    return {
        "status": "ok",
        "backfilled": len(warm_targets),
        "customskus": [
            {"customSkuId": custom_sku_id, "locale": locale}
            for _client_key, custom_sku_id, locale in warm_targets
        ],
        "unpriced": unpriced,
    }
