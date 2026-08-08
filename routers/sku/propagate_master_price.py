"""Backfill CustomSKU MSRP from a MasterSKU locale price.

A MasterSKU locale is created with ``Price: None`` (``create_master_sku.py``) and
only gets a real value later, when the DataforSEO postback lands
(``routers/enrich/dseo_webhook.py``). Any CustomSKU created in that window is
persisted with an empty ``MSRP`` — ``build_locale_data`` falls back to the master
price, which was still ``None`` — and the ``warm_widget_cache`` background task
then bails out on the zero price, so the widget quote cache is never warmed for
that SKU.

``propagate_master_price`` closes the gap: once the real price arrives it is
copied onto every CustomSKU locale entry that is still blank, and those SKUs are
returned so the caller can re-warm their widget quote cache.
"""

import logging
import os
from typing import Optional

from bson import ObjectId
from pymongo import MongoClient

logger = logging.getLogger(__name__)

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
customsku_collection = db["CustomSKU"]
clientkey_collection = db["ClientKey"]
mastersku_collection = db["MasterSKU"]

# Values that mean "no price/currency recorded yet". `None` also matches a
# missing field in a Mongo query, which is why unset entries are covered.
_BLANK_VALUES = (None, "", 0)


def _to_price(value):
    """Coerce a SERP price to a positive float, or None if it isn't usable."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def is_blank(value):
    """True for the values that mean 'not recorded' — null, missing, "" or 0."""
    return value is None or value == "" or value == 0


def _locale_entry(doc, locale):
    for entry in doc.get("Locale_Specific_Data") or []:
        if isinstance(entry, dict) and entry.get("locale") == locale:
            return entry
    return None


def _client_key_for(doc):
    """The ClientKey to price this CustomSKU under.

    Documents written by ``create_custom_sku`` carry ``Client_Key`` directly;
    older ones only have the ``Client`` id, so fall back to a ClientKey lookup.
    """
    client_key = (doc.get("Client_Key") or "").strip()
    if client_key:
        return client_key
    client_id = doc.get("Client")
    if not client_id:
        return None
    client_doc = clientkey_collection.find_one({"Client_ID": client_id}, {"ClientKey": 1})
    return (client_doc or {}).get("ClientKey")


def master_locale_price(master_sku_id, locale: str):
    """Return ``(Price, Currency)`` from a MasterSKU locale entry, or ``(None, None)``."""
    try:
        ms_id = ObjectId(master_sku_id)
    except Exception:
        return None, None
    doc = mastersku_collection.find_one({"_id": ms_id}, {"Locale_Specific_Data": 1})
    entry = _locale_entry(doc or {}, locale)
    if not entry:
        return None, None
    return entry.get("Price"), entry.get("Currency")


def propagate_master_price(master_sku_id, locale: str, price, currency=None,
                           client_id: Optional[str] = None, custom_sku_id=None):
    """Copy a newly-resolved master price onto CustomSKUs still missing an MSRP.

    Only blank MSRPs are filled — a price the client supplied at creation time is
    never overwritten. ``client_id`` narrows the update to a single client and
    ``custom_sku_id`` to a single CustomSKU; the enrichment path leaves both
    unset, because a master price applies to every SKU carrying that MasterSKU.

    Returns a list of ``(client_key, custom_sku_id, locale)`` tuples for the SKUs
    that were changed so the caller can re-warm their widget quote cache. Never
    raises: enrichment must not fail because of this.
    """
    updated = []
    resolved_price = _to_price(price)
    if not resolved_price or not locale:
        return updated

    resolved_currency = (str(currency).strip().upper() if currency else "")

    try:
        # $elemMatch keeps "this locale" and "no price" bound to the same array
        # entry, and makes the positional `$` below resolve to that entry.
        blank_msrp_match = {
            "$elemMatch": {"locale": locale, "MSRP": {"$in": list(_BLANK_VALUES)}},
        }
        query = {
            "MasterSKU": str(master_sku_id),
            "Locale_Specific_Data": blank_msrp_match,
        }
        if client_id:
            query["Client"] = client_id
        if custom_sku_id is not None:
            try:
                query["_id"] = ObjectId(custom_sku_id)
            except Exception:
                logger.warning("[MSRP-BACKFILL] invalid custom_sku_id %r", custom_sku_id)
                return updated
        candidates = customsku_collection.find(query)

        for doc in candidates:
            entry = _locale_entry(doc, locale)
            if entry is None or not is_blank(entry.get("MSRP")):
                continue

            set_ops = {"Locale_Specific_Data.$.MSRP": resolved_price}
            if resolved_currency and is_blank(entry.get("Currency")):
                set_ops["Locale_Specific_Data.$.Currency"] = resolved_currency

            # Repeat the blank-MSRP condition in the filter so this is a
            # compare-and-set: a price written between the read and the write
            # (an update_custom_sku call, a concurrent postback) wins.
            result = customsku_collection.update_one(
                {"_id": doc["_id"], "Locale_Specific_Data": blank_msrp_match},
                {"$set": set_ops},
            )
            if not result.modified_count:
                continue

            client_key = _client_key_for(doc)
            logger.info(
                "[MSRP-BACKFILL] CustomSKU %s locale=%s MSRP=%s%s",
                doc["_id"], locale, resolved_price,
                "" if client_key else " (no ClientKey — cache not warmed)",
            )
            if client_key:
                updated.append((client_key, str(doc["_id"]), locale))
    except Exception:
        logger.exception(
            "[MSRP-BACKFILL] failed for MasterSKU %s locale=%s", master_sku_id, locale
        )

    return updated
