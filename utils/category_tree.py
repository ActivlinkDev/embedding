"""Resolve a device category to its place in the ``Category`` taxonomy.

The ``Category`` collection carries three levels — ``sector`` > ``group`` >
``category`` (e.g. Home Appliances > Laundry > Washer Dryer) — plus localized
titles and an embedding. Assignment rules and rating tables match at any of the
three levels, so both need the group and sector for a device's category.
``locale_title`` carries the customer-facing name per locale ("Televisor LED"),
which :func:`localized_title` reads; the taxonomy spelling stays the key that
rules and rating tables match on.

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

import pymongo

from utils.mongo import get_client

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = int(os.getenv("CATEGORY_TREE_TTL_SECONDS", "300"))

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
    try:
        client = get_client()
    except Exception:
        logger.exception("[category-tree] could not create Mongo client")
        return None
    if client is None:
        return None
    return client[os.getenv("MONGO_DB_NAME", "Activlink")]["Category"]


def _titles(doc: dict) -> Dict[str, str]:
    """Fold ``locale_title`` into a ``{locale: title}`` map.

    The field is a list of ``{"locale": ..., "title": ...}`` in the collection.
    Entries missing either half are dropped rather than stored as blanks, so a
    half-filled row falls back like an absent one instead of rendering empty.
    """
    titles: Dict[str, str] = {}
    for entry in doc.get("locale_title") or []:
        if not isinstance(entry, dict):
            continue
        locale = str(entry.get("locale") or "").strip()
        title = str(entry.get("title") or "").strip()
        if locale and title:
            titles[locale] = title
    return titles


def _load() -> Dict[str, Dict[str, Optional[str]]]:
    """Read the taxonomy into a name -> {category, group, sector, titles} map.

    Projects the embedding away. The first document wins if a category name
    appears twice, and the duplicate is logged so it can be cleaned up.
    """
    coll = _collection()
    if coll is None:
        return {}

    tree: Dict[str, Dict[str, Optional[str]]] = {}
    # Bounded so a stuck/unhealthy Atlas can never block a request (and therefore a uvicorn
    # worker thread) indefinitely. This used to be per-client connect/socket timeouts; the
    # client is now shared process-wide, so the bound belongs on the operation instead.
    with pymongo.timeout(8):
        cursor = coll.find(
            {}, {"_id": 0, "category": 1, "group": 1, "sector": 1, "locale_title": 1}
        )
        docs = list(cursor)
    for doc in docs:
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
            "titles": _titles(doc),
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
        # Built key by key, not copied: the cache entry also carries the locale
        # titles, and this contract is exactly the three placement fields.
        return {
            "category": known.get("category") or name,
            "group": known.get("group"),
            "sector": known.get("sector"),
        }
    return {"category": name, "group": None, "sector": None}


def loaded() -> bool:
    """Whether the taxonomy could be read at all.

    Distinguishes "this category is not in the collection" from "the collection
    is unreachable" — the two look identical through :func:`resolve`, and a
    caller that rejects unknown categories must not reject every one of them
    because Mongo blipped.
    """
    return bool(_tree())


def known(category: Optional[str]) -> bool:
    """Whether the taxonomy holds this category, folding case and punctuation.

    Membership is tested against the collection itself rather than inferred from
    a resolved ``group``/``sector``: a legitimate top-level entry can carry
    neither, and would otherwise read as unknown.
    """
    name = (category or "").strip()
    return bool(name) and _key(name) in _tree()


def localized_title(category: Optional[str], locale: Optional[str]) -> str:
    """Return the customer-facing name of ``category`` in ``locale``.

    Falls back, in order, to a title for the same language in another region
    (``fr_BE`` -> ``fr_FR``), then to ``en_GB``, then to the taxonomy spelling,
    then to the caller's own string. A locale the taxonomy has no title for
    therefore reads in English rather than blank — the journey must not break on
    a missing translation.

    This is display copy only. The value that assignment rules and rating tables
    match on is :func:`resolve`'s ``category``, which is never localized.
    """
    name = (category or "").strip()
    if not name:
        return ""
    known = _tree().get(_key(name))
    canonical = (known or {}).get("category") or name
    titles = (known or {}).get("titles") or {}
    if not titles:
        return canonical

    wanted = str(locale or "").strip()
    if wanted in titles:
        return titles[wanted]
    language = wanted.split("_")[0].lower()
    if language:
        # Sorted so which region stands in for another (fr_BE -> fr_FR, not
        # fr_LU) does not depend on the order the titles were written in.
        for candidate in sorted(titles):
            if candidate.split("_")[0].lower() == language:
                return titles[candidate]
    return titles.get("en_GB") or canonical


def refresh() -> int:
    """Force a reload. Returns the number of categories now cached."""
    global _cache_loaded_at
    with _lock:
        _cache_loaded_at = 0.0
    return len(_tree())
