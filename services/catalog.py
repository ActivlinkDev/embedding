"""Canonical MasterSKU and tenant CustomSKU access.

MasterSKU owns product identity and enriched product data.  CustomSKU is a
tenant catalogue membership record containing a client SKU and explicit
overrides.  All callers should resolve catalogue data through this module so
inheritance rules stay consistent across lookup, quoting and registration.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any, Iterable, Optional

from bson import ObjectId
from pymongo.collection import Collection


SCHEMA_VERSION = 2


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def normalize_sku(value: Any) -> str:
    return normalize_text(value)


def master_match_key(make: str, model: str, gtins: Iterable[str]) -> str:
    normalized_gtins = sorted({str(v).strip() for v in gtins if str(v).strip()})
    if normalized_gtins:
        return f"gtin:{normalized_gtins[0]}"
    return f"mm:{normalize_text(make)}|{normalize_text(model)}"


def serialize(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize(item) for item in value]
    return value


def _present(mapping: dict, key: str) -> bool:
    """Whether an override exists, including an explicit null override."""
    return isinstance(mapping, dict) and key in mapping


def _take(
    target: dict,
    sources: dict,
    output_key: str,
    override: dict,
    override_key: str,
    inherited: Any,
    default: Any = None,
) -> None:
    if _present(override, override_key):
        target[output_key] = deepcopy(override[override_key])
        sources[output_key] = "custom"
    elif inherited is not None:
        target[output_key] = deepcopy(inherited)
        sources[output_key] = "master"
    else:
        target[output_key] = deepcopy(default)
        sources[output_key] = "default"


def resolve_documents(
    custom: dict,
    master: dict,
    locale: str,
    locale_defaults: Optional[dict] = None,
) -> dict:
    """Resolve a v2 CustomSKU and MasterSKU into the public product contract.

    Missing override properties inherit.  An override explicitly set to null
    remains null, allowing a tenant to suppress an inherited value.
    """
    locale_defaults = locale_defaults or {}
    identifiers = master.get("identifiers") or {}
    master_locale = (master.get("locales") or {}).get(locale) or {}
    master_market = master_locale.get("market") or {}
    master_assets = master_locale.get("assets") or {}

    overrides = custom.get("overrides") or {}
    locale_override = (overrides.get("locales") or {}).get(locale) or {}
    guarantee_override = locale_override.get("guarantee") or {}
    guarantee_sources: dict[str, str] = {}
    guarantee: dict[str, Any] = {}

    _take(
        guarantee,
        guarantee_sources,
        "partsMonths",
        guarantee_override,
        "partsMonths",
        None,
        locale_defaults.get("gtee_parts", 0),
    )
    _take(
        guarantee,
        guarantee_sources,
        "labourMonths",
        guarantee_override,
        "labourMonths",
        None,
        locale_defaults.get("gtee_labour", 0),
    )

    product: dict[str, Any] = {
        "make": identifiers.get("make") or "",
        "model": identifiers.get("model") or "",
        "gtins": list(identifiers.get("gtins") or []),
        "sku": custom.get("sku") or "",
        "imageUrl": master.get("imageUrl") or master_assets.get("primaryImage"),
        "assets": deepcopy(master_assets),
        "market": deepcopy(master_market),
        "specifications": deepcopy(master_locale.get("specifications") or {}),
    }
    field_sources: dict[str, Any] = {
        "make": "master",
        "model": "master",
        "gtins": "master",
        "sku": "custom",
        "imageUrl": "master",
        "assets": "master",
        "market": "master",
        "specifications": "master",
        "guarantee": guarantee_sources,
    }

    category_override = locale_override if _present(locale_override, "category") else overrides
    _take(
        product,
        field_sources,
        "category",
        category_override,
        "category",
        master_locale.get("category") or master.get("category"),
        "",
    )
    _take(product, field_sources, "title", locale_override, "title", master_locale.get("title"), "")
    _take(
        product,
        field_sources,
        "price",
        locale_override,
        "price",
        master_market.get("referencePrice"),
        0,
    )
    _take(
        product,
        field_sources,
        "currency",
        locale_override,
        "currency",
        master_market.get("currency"),
        locale_defaults.get("currency", ""),
    )
    _take(product, field_sources, "customLinks", locale_override, "customLinks", None, [])
    _take(product, field_sources, "generateOffers", locale_override, "generateOffers", None, True)
    _take(product, field_sources, "localePromotion", locale_override, "promotion", None, None)
    _take(product, field_sources, "globalPromotion", overrides, "globalPromotion", None, None)
    product["guarantee"] = guarantee

    return {
        "customSkuId": str(custom.get("_id")),
        "masterSkuId": str(master.get("_id")),
        "clientId": custom.get("clientId"),
        "schemaVersion": SCHEMA_VERSION,
        "locale": locale,
        "enabledLocales": list(custom.get("enabledLocales") or []),
        "sources": list(custom.get("sources") or []),
        "product": product,
        "overrides": deepcopy(overrides),
        "fieldSources": field_sources,
    }


class CatalogService:
    def __init__(
        self,
        master_collection: Collection,
        custom_collection: Collection,
        locale_collection: Collection,
        client_collection: Collection,
    ) -> None:
        self.masters = master_collection
        self.customs = custom_collection
        self.locales = locale_collection
        self.clients = client_collection

    def ensure_indexes(self) -> None:
        self.masters.create_index("matchKey", unique=True, name="uniq_master_match_key")
        self.masters.create_index("identifiers.gtins", name="master_gtins")
        self.masters.create_index(
            [("identifiers.makeNormalized", 1), ("identifiers.modelNormalized", 1)],
            name="master_make_model",
        )
        self.customs.create_index(
            [("clientId", 1), ("skuNormalized", 1)],
            unique=True,
            name="uniq_client_sku",
        )
        self.customs.create_index(
            [("clientId", 1), ("masterSkuId", 1)],
            name="client_master",
        )
        self.customs.create_index(
            [("clientId", 1), ("enabledLocales", 1)],
            name="client_locales",
        )

    def client_for_key(self, client_key: str) -> Optional[dict]:
        return self.clients.find_one({"ClientKey": client_key})

    def locale_defaults(self, locale: str) -> dict:
        return self.locales.find_one({"locale": locale}, {"_id": 0}) or {}

    def find_master(
        self,
        *,
        master_id: Optional[str] = None,
        gtin: Optional[str] = None,
        make: Optional[str] = None,
        model: Optional[str] = None,
    ) -> tuple[Optional[dict], Optional[str]]:
        if master_id:
            try:
                doc = self.masters.find_one({"_id": ObjectId(master_id)})
            except Exception:
                raise ValueError("Invalid masterSkuId")
            return doc, "masterSkuId" if doc else None
        if gtin:
            doc = self.masters.find_one({"identifiers.gtins": str(gtin).strip()})
            if doc:
                return doc, "gtin"
        if make and model:
            doc = self.masters.find_one(
                {
                    "identifiers.makeNormalized": normalize_text(make),
                    "identifiers.modelNormalized": normalize_text(model),
                }
            )
            return doc, "make+model" if doc else None
        return None, None

    def find_custom(
        self,
        *,
        client_id: Any,
        locale: Optional[str] = None,
        custom_id: Optional[str] = None,
        sku: Optional[str] = None,
        master_id: Optional[ObjectId] = None,
    ) -> tuple[Optional[dict], Optional[str]]:
        base: dict[str, Any] = {"clientId": client_id}
        if locale:
            base["enabledLocales"] = locale
        if custom_id:
            try:
                base["_id"] = ObjectId(custom_id)
            except Exception:
                raise ValueError("Invalid customSkuId")
            return self.customs.find_one(base), "customSkuId"
        if sku:
            base["skuNormalized"] = normalize_sku(sku)
            return self.customs.find_one(base), "sku"
        if master_id:
            base["masterSkuId"] = master_id
            return self.customs.find_one(base), "master"
        return None, None

    def resolve_custom(self, custom: dict, locale: str) -> Optional[dict]:
        master_id = custom.get("masterSkuId")
        if not isinstance(master_id, ObjectId):
            return None
        master = self.masters.find_one({"_id": master_id})
        if not master:
            return None
        return resolve_documents(custom, master, locale, self.locale_defaults(locale))

    def resolve_lookup(
        self,
        *,
        client_key: str,
        locale: str,
        custom_id: Optional[str] = None,
        sku: Optional[str] = None,
        gtin: Optional[str] = None,
        make: Optional[str] = None,
        model: Optional[str] = None,
    ) -> tuple[Optional[dict], Optional[str]]:
        client = self.client_for_key(client_key)
        if not client or "Client_ID" not in client:
            raise LookupError("Invalid clientKey")
        client_id = client["Client_ID"]

        custom, matched_by = self.find_custom(
            client_id=client_id,
            locale=locale,
            custom_id=custom_id,
            sku=sku if not custom_id else None,
        )
        if not custom and not custom_id:
            master, master_match = self.find_master(gtin=gtin, make=make, model=model)
            if master:
                custom, _ = self.find_custom(
                    client_id=client_id,
                    locale=locale,
                    master_id=master["_id"],
                )
                matched_by = master_match
        if not custom:
            return None, None
        return self.resolve_custom(custom, locale), matched_by

    def resolved_for_id(self, custom_id: str, client_key: str, locale: str) -> Optional[dict]:
        resolved, _ = self.resolve_lookup(
            client_key=client_key,
            locale=locale,
            custom_id=custom_id,
        )
        return resolved
