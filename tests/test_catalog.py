from bson import ObjectId

from services.catalog import master_match_key, resolve_documents


def documents():
    master = {
        "_id": ObjectId(),
        "identifiers": {
            "make": "Bosch",
            "model": "ABC-1",
            "gtins": ["5012345678900"],
        },
        "category": "Appliance",
        "imageUrl": "https://example.test/product.png",
        "locales": {
            "en_GB": {
                "title": "Canonical title",
                "category": "Dishwasher",
                "market": {"referencePrice": 499.0, "currency": "GBP"},
                "assets": {"documents": []},
                "specifications": {"width": "600 mm"},
            }
        },
    }
    custom = {
        "_id": ObjectId(),
        "clientId": "client-1",
        "masterSkuId": master["_id"],
        "sku": "CLIENT-001",
        "enabledLocales": ["en_GB"],
        "sources": ["web"],
        "overrides": {"locales": {}},
    }
    return custom, master


def test_missing_overrides_inherit_master_and_locale_defaults():
    custom, master = documents()
    result = resolve_documents(
        custom,
        master,
        "en_GB",
        {"gtee_parts": 24, "gtee_labour": 12},
    )

    assert result["product"]["title"] == "Canonical title"
    assert result["product"]["price"] == 499.0
    assert result["product"]["guarantee"] == {
        "partsMonths": 24,
        "labourMonths": 12,
    }
    assert result["fieldSources"]["title"] == "master"


def test_custom_values_override_master():
    custom, master = documents()
    custom["overrides"] = {
        "category": "Kitchen",
        "globalPromotion": "GLOBAL",
        "locales": {
            "en_GB": {
                "title": "Retailer title",
                "price": 425.0,
                "guarantee": {"partsMonths": 36},
                "promotion": "LOCAL",
            }
        },
    }

    result = resolve_documents(custom, master, "en_GB", {"gtee_labour": 12})

    assert result["product"]["title"] == "Retailer title"
    assert result["product"]["price"] == 425.0
    assert result["product"]["category"] == "Kitchen"
    assert result["product"]["guarantee"]["partsMonths"] == 36
    assert result["product"]["guarantee"]["labourMonths"] == 12
    assert result["product"]["localePromotion"] == "LOCAL"
    assert result["product"]["globalPromotion"] == "GLOBAL"
    assert result["fieldSources"]["price"] == "custom"


def test_explicit_null_suppresses_an_inherited_value():
    custom, master = documents()
    custom["overrides"]["locales"]["en_GB"] = {"title": None}

    result = resolve_documents(custom, master, "en_GB")

    assert result["product"]["title"] is None
    assert result["fieldSources"]["title"] == "custom"


def test_match_key_prefers_gtin_and_normalizes_make_model_fallback():
    assert master_match_key("Any", "Model", ["2", "1"]) == "gtin:1"
    assert master_match_key("  Bosch ", "ABC   1", []) == "mm:bosch|abc 1"


def test_ignore_overrides_resolves_the_inherited_baseline():
    """The admin portal shows what a field falls back to when its override goes.

    ``resolved`` alone cannot answer that: once an override is set it replaces
    the inherited value, so the baseline has to be resolved separately.
    """
    from services.catalog import CatalogService

    custom, master = documents()
    custom["overrides"] = {
        "category": "Kitchen",
        "locales": {"en_GB": {"title": "Retailer title", "price": 425.0}},
    }

    class _Masters:
        def find_one(self, _query):
            return master

    class _Locales:
        def find_one(self, _query, _projection=None):
            return {"locale": "en_GB", "gtee_parts": 24, "gtee_labour": 12}

    service = CatalogService(_Masters(), None, _Locales(), None)

    overridden = service.resolve_custom(custom, "en_GB")
    baseline = service.resolve_custom(custom, "en_GB", ignore_overrides=True)

    assert overridden["product"]["title"] == "Retailer title"
    assert overridden["product"]["category"] == "Kitchen"
    assert baseline["product"]["title"] == "Canonical title"
    assert baseline["product"]["price"] == 499.0
    assert baseline["product"]["category"] == "Dishwasher"
    assert baseline["fieldSources"]["title"] == "master"
    # The caller's document is untouched — the baseline resolves against a copy.
    assert custom["overrides"]["category"] == "Kitchen"
