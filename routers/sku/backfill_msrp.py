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
    ClientKey: str
    id: Optional[str] = Field(None, description="Single CustomSKU id; omit to sweep the whole client")
    Locale: Optional[str] = Field(None, description="Restrict to one locale; omit for all")


@router.post("/backfill_msrp")
def backfill_msrp(
    data: BackfillMSRPRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_token),
):
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
            propagate_master_price(master_sku_id, locale, price, currency, client_id=client_id)
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
