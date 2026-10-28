"""Offer-page copy and damage examples for a device's category.

The CMS ``category-offer`` collection holds a headline, intro and a list of
illustrative damage examples per taxonomy name, at any of the three levels the
``Category`` collection carries (sector > group > category). This endpoint
picks the entry for one device the same way rating picks a factor: the most
specific level wins, so a sector entry can set a default and a category entry
override it.

Locale fallback runs *inside* each level — requested locale, then another
region of the same language, then en-GB — before moving to a broader level.
Category-specific copy in English is more useful than generic sector copy in
the customer's language, and the storefront shows the same examples either
way.

Copy is display-only and fails safe: an unknown category, a missing entry or
an unreachable CMS all return ``{"data": null}``, and the offer page renders
without the block. Only published entries are visible, so drafts awaiting
compliance review never reach a customer.
"""

import logging
import os
import time
from typing import Callable, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from utils import category_tree
from utils.api_docs import error
from utils.dependencies import verify_token
from utils.locale import (
    LocaleNotSupportedError,
    extract_language_code,
    get_locale_mapping,
    resolve_strapi_locale,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["CMS"])

STRAPI_API_BASE = (os.getenv("STRAPI_BASE_URL") or "https://strapi-production-5603.up.railway.app/api").rstrip("/")
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")
CACHE_TTL_SECONDS = int(os.getenv("CATEGORY_OFFER_TTL_SECONDS", "300"))
DEFAULT_CMS_LOCALE = "en-GB"
LEVELS = ("category", "group", "sector")
COVER_TYPES = {"breakdown", "accidental"}
MAX_CATEGORY_LENGTH = 100

# strapi locale -> (loaded_at, entries). Small: one list of tens of entries per locale.
_cache: Dict[str, tuple] = {}


def _clean(value) -> str:
    return str(value or "").strip()


def _normalise(doc: dict) -> Optional[dict]:
    """One CMS entry in the shape this API returns, or None if it is unusable.

    An example whose cover type the CMS does not recognise is treated as
    accidental: the storefront hides accidental examples from breakdown-only
    plans, so an unknown value errs towards showing less, never more.
    """
    name = _clean(doc.get("Taxonomy_Name"))
    level = _clean(doc.get("Level")).lower()
    if not name or level not in LEVELS:
        return None
    examples = []
    for ex in doc.get("Damage_Examples") or []:
        if not isinstance(ex, dict) or not _clean(ex.get("Title")):
            continue
        cover_type = _clean(ex.get("Cover_Type")).lower()
        examples.append({
            "title": _clean(ex.get("Title")),
            "description": _clean(ex.get("Description")),
            "cover_type": cover_type if cover_type in COVER_TYPES else "accidental",
            "icon": _clean(ex.get("Icon")) or None,
        })
    return {
        "taxonomy_name": name,
        "level": level,
        "client": _clean(doc.get("Client")) or None,
        "locale": _clean(doc.get("locale")) or None,
        "headline": _clean(doc.get("Headline")) or None,
        "intro": _clean(doc.get("Intro")) or None,
        "examples_heading": _clean(doc.get("Examples_Heading")) or None,
        "examples": examples,
    }


async def _fetch_locale(strapi_locale: str) -> List[dict]:
    """Every published category-offer entry in one CMS locale."""
    headers = {"Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"} if STRAPI_BEARER_TOKEN else {}
    entries: List[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=10.0) as client_http:
        while True:
            response = await client_http.get(
                f"{STRAPI_API_BASE}/category-offers",
                params={
                    "locale": strapi_locale,
                    "populate": "Damage_Examples",
                    "pagination[page]": page,
                    "pagination[pageSize]": 100,
                },
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
            for doc in body.get("data") or []:
                entry = _normalise(doc.get("attributes", doc) if isinstance(doc, dict) else {})
                if entry:
                    entries.append(entry)
            page_count = ((body.get("meta") or {}).get("pagination") or {}).get("pageCount") or 1
            if page >= page_count:
                return entries
            page += 1


async def _entries_for_locale(strapi_locale: str) -> List[dict]:
    """The cached entries for a locale, refreshed on a TTL.

    A failed refresh keeps serving the previous list — a CMS blip should not
    blank the copy on every offer page — and a locale never loaded reads as
    empty, which the caller treats as "no copy".
    """
    cached = _cache.get(strapi_locale)
    if cached and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        entries = await _fetch_locale(strapi_locale)
    except Exception as exc:
        logger.warning("[category-offer] could not load %s from the CMS: %s", strapi_locale, exc)
        return cached[1] if cached else []
    _cache[strapi_locale] = (time.monotonic(), entries)
    return entries


def locale_chain(strapi_locale: str) -> List[str]:
    """Requested locale, then other regions of its language (sorted), then en-GB.

    The same order ``category_tree.localized_title`` uses for category names,
    so ``fr-BE`` reads French from ``fr-FR`` rather than dropping to English.
    """
    chain = [strapi_locale]
    language = extract_language_code(strapi_locale)
    siblings = sorted(
        {m["cms"] for m in get_locale_mapping().values() if extract_language_code(m["cms"]) == language}
    )
    for code in siblings + [DEFAULT_CMS_LOCALE]:
        if code not in chain:
            chain.append(code)
    return chain


def _candidates(placement: Dict[str, Optional[str]], client: Optional[str]) -> List[tuple]:
    """(level, folded name, client) in priority order: specific level first, client before generic."""
    out = []
    for level in LEVELS:
        name = placement.get(level)
        if not name:
            continue
        key = category_tree._key(name)
        if client:
            out.append((level, key, client))
        out.append((level, key, None))
    return out


async def select_offer(
    category: str,
    strapi_locale: str,
    client: Optional[str],
    entries_for: Callable = None,
) -> Optional[dict]:
    """The best entry for a category, or None.

    Walks level by level (category, group, sector); within a level a client
    override beats the generic entry, and each is tried across the locale
    chain before the next candidate.
    """
    entries_for = entries_for or _entries_for_locale
    placement = category_tree.resolve(category)
    client_key = (client or "").strip().lower() or None
    chain = locale_chain(strapi_locale)
    loaded: Dict[str, List[dict]] = {}

    for level, key, cand_client in _candidates(placement, client_key):
        for code in chain:
            if code not in loaded:
                loaded[code] = await entries_for(code)
            for entry in loaded[code]:
                if entry["level"] != level or category_tree._key(entry["taxonomy_name"]) != key:
                    continue
                if (entry["client"] or "").lower() != (cand_client or ""):
                    continue
                if not entry["headline"] and not entry["examples"]:
                    continue
                return {**entry, "locale": entry["locale"] or code}
    return None


@router.get(
    "/category_offer",
    summary="Fetch offer-page copy and damage examples for a device category",
    response_description="The most specific copy available, or null.",
    responses={
        200: {
            "description": (
                "`data` is the matched entry, or `null` when no entry covers the category at "
                "any level. Examples carry `cover_type` (`breakdown` or `accidental`); show "
                "accidental ones only for plans that cover accidental damage."
            ),
            "content": {"application/json": {"example": {"data": {
                "taxonomy_name": "Smartphone", "level": "category", "client": None, "locale": "en-GB",
                "headline": "Cover for your phone",
                "intro": "Here are some common problems this plan can help with.",
                "examples_heading": "Examples of what's covered",
                "examples": [{
                    "title": "Cracked screen from a drop",
                    "description": "Your phone slips out of your hand and the screen cracks.",
                    "cover_type": "accidental", "icon": "crack",
                }],
            }}}},
        },
        400: error("The category is empty or too long, or the locale has no CMS equivalent.", "Invalid category"),
    },
)
async def category_offer(
    request: Request,
    locale: str = Query(..., description="**Mandatory.** Locale in underscore form (`fr_BE`).", examples=["fr_BE"]),
    category: str = Query(..., description="**Mandatory.** The device category from the rate response.", examples=["Smartphone"]),
    clientKey: Optional[str] = Query(
        None,
        description=(
            "Optional. Prefer this client's override entries. A caller pinned to a ClientKey "
            "always gets its own overrides, whatever is sent here."
        ),
    ),
    _: None = Depends(verify_token),
):
    """
    Return the category-specific headline, intro and damage examples for the offer page.

    The category is resolved through the `Category` taxonomy, then the CMS is searched at
    category, group and sector level in that order. Within each level a client override wins
    over the generic entry, and the locale falls back to another region of the same language,
    then `en-GB`. Nothing found — or the CMS unreachable — returns `{"data": null}` so the page
    can render without the block.
    """
    category = (category or "").strip()
    if not category or len(category) > MAX_CATEGORY_LENGTH:
        raise HTTPException(status_code=400, detail="Invalid category")
    try:
        _, strapi_locale = resolve_strapi_locale(locale)
    except LocaleNotSupportedError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # verify_token has already refused a pinned caller naming another client; a pinned
    # caller that names none still gets its own overrides.
    caller = getattr(request.state, "caller", {}) or {}
    client = caller.get("client_key") or clientKey

    try:
        return {"data": await select_offer(category, strapi_locale, client)}
    except Exception:
        logger.exception("[category-offer] lookup failed for %r in %s", category, strapi_locale)
        return {"data": None}
