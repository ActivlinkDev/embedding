"""Resolve a device category to its place in the ``Category`` taxonomy.

The ``Category`` collection carries three levels — ``sector`` > ``group`` >
``category`` (e.g. Home Appliances > Laundry > Washer Dryer) — plus localized
titles and an embedding. Assignment rules and rating tables match at any of the
three levels, so both need the group and sector for a device's category.

The collection is small (tens of documents) and changes rarely, so it is cached
in-process and refreshed on a TTL. The embedding field is *never* fetched: it is
3072 floats per document and nothing here needs it.

Nothing in this module raises. A category the collection does not know about
resolves to itself with no group or sector, so category-level rules still match
it and broader rules simply do not.
"""

import logging
import os
import re
import threading
import time
from typing import Dict, Optional

from pymongo import MongoClient

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = int(os.getenv("CATEGORY_TREE_TTL_SECONDS", "300"))

_client = None
_cache: Dict[str, Dict[str, Optional[str]]] = {}
_cache_loaded_at = 0.0
_lock = threading.Lock()


def _key(value: Optional[str]) -> str:
    """Fold a category name for lookup: case and punctuation insensitive.

    Device categories reach us from free-text admin fields and from the
    classifier, so "Fridge Freezer", "fridge-freezer" and "Fridge/Freezer"
    should all find the same taxonomy entry.
    """
    return re.sub(r"\W+", "", (value or "")).strip().lower()


def _collection():
    """The ``Category`` collection, or None when Mongo is unreachable."""
    global _client
    if _client is None:
        uri = os.getenv("MONGO_URI")
        if not uri:
            return None
        try:
            _client = MongoClient(
                uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=8000,
            )
        except Exception:
            logger.exception("[category-tree] could not create Mongo client")
            return None
    return _client[os.getenv("MONGO_DB_NAME", "Activlink")]["Category"]


def _load() -> Dict[str, Dict[str, Optional[str]]]:
    """Read the taxonomy into a name -> {category, group, sector} map.

    Projects the embedding away. The first document wins if a category name
    appears twice, and the duplicate is logged so it can be cleaned up.
    """
    coll = _collection()
    if coll is None:
        return {}

    tree: Dict[str, Dict[str, Optional[str]]] = {}
    for doc in coll.find({}, {"_id": 0, "category": 1, "group": 1, "sector": 1}):
        name = (doc.get("category") or "").strip()
        if not name:
            continue
        key = _key(name)
        if key in tree:
            logger.warning(
                "[category-tree] duplicate category %r in the Category collection; "
                "keeping the first (group=%r sector=%r) and ignoring group=%r sector=%r",
                name, tree[key]["group"], tree[key]["sector"],
                doc.get("group"), doc.get("sector"),
            )
            continue
        tree[key] = {
            "category": name,
            "group": (doc.get("group") or None),
            "sector": (doc.get("sector") or None),
        }
    return tree


def _tree() -> Dict[str, Dict[str, Optional[str]]]:
    """The cached taxonomy, reloaded when the TTL has passed.

    A failed reload keeps serving the previous cache rather than emptying it —
    a Mongo blip should not silently narrow every rule to category level.
    """
    global _cache, _cache_loaded_at
    with _lock:
        if _cache and (time.monotonic() - _cache_loaded_at) < CACHE_TTL_SECONDS:
            return _cache
        try:
            loaded = _load()
        except Exception:
            logger.exception("[category-tree] reload failed; serving the previous cache")
            return _cache
        if loaded:
            _cache = loaded
            _cache_loaded_at = time.monotonic()
        elif _cache:
            logger.warning("[category-tree] reload returned nothing; keeping the previous cache")
        return _cache


def resolve(category: Optional[str]) -> Dict[str, Optional[str]]:
    """Return ``{category, group, sector}`` for a category name.

    Lookup is case and punctuation insensitive, and the ``category`` returned is
    the taxonomy's own spelling — so callers can compare it against rule lists
    with plain equality.

    ``group`` and ``sector`` are None when the name is not in the collection,
    which is what a free-typed category or an "Unknown" classification looks
    like. Such a device can still match rules that name its category directly.
    """
    name = (category or "").strip()
    if not name:
        return {"category": "", "group": None, "sector": None}
    known = _tree().get(_key(name))
    if known:
        return dict(known)
    return {"category": name, "group": None, "sector": None}


def refresh() -> int:
    """Force a reload. Returns the number of categories now cached."""
    global _cache_loaded_at
    with _lock:
        _cache_loaded_at = 0.0
    return len(_tree())
