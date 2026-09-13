"""Type-ahead search over the canonical MasterSKU catalogue.

The portal's Create SKU form asks for GTIN / Make / Model before it creates a
CustomSKU, and each of those values normally reaches `create_master_sku`, which
pays for an Icecat or Go-UPC round trip and can resolve a slightly different
make or model than the one already stored. This endpoint lets the form find the
MasterSKU that already exists and link straight to it, so an operator imports a
canonical product instead of re-enriching it.

MasterSKU is the platform-wide product catalogue, not tenant data: it holds
identifiers, enriched product copy and market prices, all of which any client
already sees on any product card resolved from it, and `create_custom_sku`
already attaches any client to any master matched by GTIN or make+model. The
one piece of tenant data here is `existing`, which reports whether the caller's
*own* client already has a CustomSKU for a master; it is filled in only from
the Client_ID behind the supplied clientKey, so it can never describe another
tenant's catalogue.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from services.catalog import normalize_text, serialize
from utils.dependencies import verify_token
from .catalog_dependencies import catalog, custom_collection, master_collection


router = APIRouter(prefix="/sku", tags=["Catalog"])

# Long enough that a single keystroke cannot ask Mongo to scan the catalogue,
# short enough for a two-letter make ("AO", "LG") to be searchable.
MIN_QUERY_LENGTH = 2


def _prefix(value: str) -> dict:
    """Anchored, case-insensitive match.

    Anchoring matters for more than relevance: `identifiers.makeNormalized`,
    `identifiers.modelNormalized` and `identifiers.gtins` are indexed, and only
    a prefix regex can use an index — an unanchored one degrades to a scan of
    every product ever enriched.
    """
    return {"$regex": f"^{re.escape(value)}", "$options": "i"}


def _filter(field: str, search: str) -> dict:
    normalized = normalize_text(search)
    if field == "gtin":
        return {"identifiers.gtins": _prefix(search)}
    if field == "make":
        return {"identifiers.makeNormalized": _prefix(normalized)}
    if field == "model":
        return {"identifiers.modelNormalized": _prefix(normalized)}
    return {"$or": [
        {"identifiers.gtins": _prefix(search)},
        {"identifiers.makeNormalized": _prefix(normalized)},
        {"identifiers.modelNormalized": _prefix(normalized)},
    ]}


def _suggestion(master: dict, locale: Optional[str]) -> dict:
    """One row of the type-ahead list.

    `localeTitles` comes from the projection below as ``[{"k": locale,
    "title": ...}]`` — the locale blocks are keyed by locale code, so their
    titles cannot be reached by a fixed projection path.
    """
    identifiers = master.get("identifiers") or {}
    titles = {
        str(entry.get("k")): str(entry.get("title") or "").strip()
        for entry in (master.get("localeTitles") or [])
        if isinstance(entry, dict) and entry.get("k")
    }
    identity = " ".join(
        part for part in (identifiers.get("make"), identifiers.get("model")) if part
    )
    # Prefer the requested locale's title, then any locale's, then the
    # identifiers — a master enriched for one locale only must still be
    # recognisable in the list for another.
    title = (titles.get(locale or "") or next((t for t in titles.values() if t), "")) or identity
    return {
        "masterSkuId": str(master.get("_id")),
        "make": identifiers.get("make") or "",
        "model": identifiers.get("model") or "",
        "gtins": list(identifiers.get("gtins") or []),
        "category": master.get("category") or "",
        "title": title,
        "imageUrl": master.get("imageUrl"),
        # Which locales already carry enriched copy. A locale missing here is
        # not an error — creating a CustomSKU for it adds the block — but the
        # portal needs to know, because `create_custom_sku` rejects an explicit
        # masterSkuId for a locale the master does not have yet.
        "locales": sorted(titles.keys()),
    }


def _owned_by_client(client_id: Any, master_ids: list[str]) -> dict[str, dict]:
    """The caller's own CustomSKUs for these masters, keyed by masterSkuId.

    One query for the whole page of results rather than one per row. A client
    can hold more than one SKU against the same master; the first by SKU order
    is enough to tell the operator the product is already in their catalogue.
    """
    object_ids = []
    for value in master_ids:
        try:
            object_ids.append(ObjectId(value))
        except Exception:
            continue
    if not object_ids:
        return {}
    owned = custom_collection.find(
        {"clientId": client_id, "masterSkuId": {"$in": object_ids}},
        {"masterSkuId": 1, "sku": 1, "enabledLocales": 1},
    ).sort([("skuNormalized", 1), ("_id", 1)])
    by_master: dict[str, dict] = {}
    for custom in owned:
        by_master.setdefault(str(custom.get("masterSkuId")), {
            "customSkuId": str(custom.get("_id")),
            "sku": custom.get("sku") or "",
            "enabledLocales": list(custom.get("enabledLocales") or []),
        })
    return by_master


@router.get("/master_search")
def master_search(
    q: str = Query(...),
    field: str = Query("any", pattern="^(any|gtin|make|model)$"),
    clientKey: Optional[str] = Query(None),
    locale: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    _: None = Depends(verify_token),
):
    search = (q or "").strip()
    if len(search) < MIN_QUERY_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"q must be at least {MIN_QUERY_LENGTH} characters",
        )

    client_id: Any = None
    if clientKey:
        client = catalog.client_for_key(clientKey)
        if not client or "Client_ID" not in client:
            raise HTTPException(status_code=404, detail="Invalid clientKey")
        client_id = client["Client_ID"]

    masters = list(master_collection.aggregate([
        {"$match": _filter(field, search)},
        {"$sort": {"identifiers.makeNormalized": 1, "identifiers.modelNormalized": 1, "_id": 1}},
        {"$limit": limit},
        # Market blocks, specifications and asset galleries are large and the
        # form shows none of it, so only the identity fields and the locale
        # titles are read back.
        {"$project": {
            "identifiers": 1,
            "category": 1,
            "imageUrl": 1,
            "localeTitles": {
                "$map": {
                    "input": {"$objectToArray": {"$ifNull": ["$locales", {}]}},
                    "as": "entry",
                    "in": {"k": "$$entry.k", "title": "$$entry.v.title"},
                }
            },
        }},
    ]))

    results = [_suggestion(master, locale) for master in masters]

    if client_id is not None and results:
        by_master = _owned_by_client(client_id, [item["masterSkuId"] for item in results])
        for item in results:
            item["existing"] = by_master.get(item["masterSkuId"])

    return serialize({"count": len(results), "results": results})
