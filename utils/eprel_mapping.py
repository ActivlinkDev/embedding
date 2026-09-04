"""Mapping from EPREL product groups to Activlink categories and CustomSKU input.

The productGroup -> category mapping lives in the EPREL_Category_Map collection
rather than in this module, so it can be corrected without a deploy as the
Category collection evolves. DEFAULT_GROUP_NAMES below is seed data only, used
by scripts/seed_eprel_category_map.py to populate that collection.

A mapping document looks like:

    {"productGroup": "washingmachines2019",
     "name": "Washing Machine",      # text fed to the embedding matcher
     "category": "Washing Machine",  # optional pinned canonical category
     "enabled": True}
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Seed data only — the live source of truth is the EPREL_Category_Map collection.
DEFAULT_GROUP_NAMES = {
    "airconditioners": "Air Conditioner",
    "dishwashers": "Dishwasher",
    "dishwashers2019": "Dishwasher",
    "electronicdisplays": "Television",
    "hotwaterstoragetanks": "Water Heater",
    "lamps": "Light Bulb",
    "lightsources": "Light Bulb",
    "localspaceheaters": "Heater",
    "ovens": "Oven",
    "professionalrefrigeratedstoragecabinets": "Professional Refrigerated Cabinet",
    "rangehoods": "Cooker Hood",
    "refrigeratingappliances": "Refrigerator Freezer",
    "refrigeratingappliances2019": "Refrigerator Freezer",
    "refrigeratingappliancesdirectsalesfunction": "Commercial Refrigerator",
    "residentialventilationunits": "Ventilation Unit",
    "smartphonestablets20231669": "Smartphone Tablet",
    "solidfuelboilerpackages": "Boiler",
    "solidfuelboilers": "Boiler",
    "spaceheaterpackages": "Heater",
    "spaceheaters": "Heater",
    "spaceheatersolardevice": "Heater",
    "spaceheatertemperaturecontrol": "Heater",
    "televisions": "Television",
    "tumbledriers": "Tumble Dryer",
    "tumbledryers20232534": "Tumble Dryer",
    "tyres": "Tyres",
    "washerdriers": "Washer Dryer",
    "washerdriers2019": "Washer Dryer",
    "washingmachines": "Washing Machine",
    "washingmachines2019": "Washing Machine",
    "waterheaterpackages": "Water Heater",
    "waterheaters": "Water Heater",
    "waterheatersolardevices": "Water Heater",
}

# Reasons returned alongside a failed resolution, so callers can tell a missing
# configuration apart from a genuine no-match.
REASON_OK = "ok"
REASON_NO_MAPPING = "no mapping configured for product group"
REASON_DISABLED = "product group disabled in category map"
REASON_NO_MATCH = "no category matched"


def resolve_category(map_collection, product_group: str, cache: dict) -> Tuple[Optional[str], str]:
    """Resolve an EPREL product group to a canonical Activlink category.

    Returns (category_or_None, reason). Results are memoised in `cache` so a
    long import performs at most one resolution per product group.
    """
    if product_group in cache:
        return cache[product_group]

    result = _resolve_uncached(map_collection, product_group)
    cache[product_group] = result
    return result


def _resolve_uncached(map_collection, product_group: str) -> Tuple[Optional[str], str]:
    mapping = map_collection.find_one({"productGroup": product_group})
    if not mapping:
        return None, REASON_NO_MAPPING
    if not mapping.get("enabled", True):
        return None, REASON_DISABLED

    # A pinned category wins: deterministic, and no embedding call needed.
    pinned = (mapping.get("category") or "").strip()
    if pinned:
        return pinned, REASON_OK

    name = (mapping.get("name") or "").strip()
    if not name:
        return None, REASON_NO_MAPPING

    # Reuse the same matcher create_master_sku uses, so importer categories are
    # resolved exactly the way manually created SKUs are.
    from routers.sku.create_master_sku import compute_category_embedding

    final_category, _matched, similarity, _embedding = compute_category_embedding(name)
    if not final_category or final_category == "Unknown":
        logger.info(
            "[eprel_mapping] no category for group=%s name=%r similarity=%s",
            product_group, name, similarity,
        )
        return None, REASON_NO_MATCH

    # Pin the resolved value so the group costs nothing to resolve next time.
    try:
        map_collection.update_one(
            {"productGroup": product_group},
            {"$set": {"category": final_category}},
        )
    except Exception:
        logger.exception("[eprel_mapping] failed to pin category for %s", product_group)

    return final_category, REASON_OK


def make_from(doc: dict) -> str:
    """Best available brand name for an EPREL record."""
    organisation = doc.get("organisation") or {}
    return (
        doc.get("supplierOrTrademark")
        or doc.get("trademarkOwner")
        or organisation.get("organisationName")
        or ""
    ).strip()


def eprel_to_custom_sku_request(doc: dict, client_key: str, locale: str, category: str,
                                add_pricing: bool):
    """Build a CustomSKURequest for one EPREL record in one locale.

    EPREL publishes no GTIN, so this always takes the Make+Model path. The
    manufacturer warranty (guaranteeDuration, in months) populates the guarantee
    fields when present; otherwise the locale defaults apply.
    """
    from routers.sku.create_custom_sku import CustomSKURequest, LocaleDetails

    model = (doc.get("modelIdentifier") or "").strip()
    guarantee = doc.get("guaranteeDuration")
    locale_details = None
    if isinstance(guarantee, int) and guarantee > 0:
        locale_details = LocaleDetails(GTL=guarantee, GTP=guarantee)

    return CustomSKURequest(
        ClientKey=client_key,
        Locale=locale,
        SKU=model,
        Source="EPREL",
        Make=make_from(doc),
        Model=model,
        Category=category,
        Locale_Details=locale_details,
        add_pricing=add_pricing,
    )
