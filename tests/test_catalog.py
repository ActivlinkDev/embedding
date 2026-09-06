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
                "categoryTitle": "Dishwasher",
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


def test_locale_category_title_is_localized_while_the_category_is_not():
    """Rating matches on `category`; only `categoryTitle` carries the translation."""
    custom, master = documents()
    master["locales"]["es_ES"] = {
        "title": "T\u00edtulo",
        "category": "Dishwasher",
        "categoryTitle": "Lavavajillas",
    }
    custom["enabledLocales"] = ["en_GB", "es_ES"]

    result = resolve_documents(custom, master, "es_ES")

    assert result["product"]["category"] == "Dishwasher"
    assert result["product"]["categoryTitle"] == "Lavavajillas"


def test_category_title_falls_back_to_the_category_before_the_backfill():
    """A MasterSKU written before locale titles existed must still render a name."""
    custom, master = documents()
    del master["locales"]["en_GB"]["categoryTitle"]

    result = resolve_documents(custom, master, "en_GB")

    assert result["product"]["categoryTitle"] == "Dishwasher"


def test_a_category_override_names_its_own_title():
    """A tenant that renames the category has named what the card renders too."""
    custom, master = documents()
    custom["overrides"] = {"locales": {"en_GB": {"category": "Kitchen"}}}

    result = resolve_documents(custom, master, "en_GB")

    assert result["product"]["category"] == "Kitchen"
    assert result["product"]["categoryTitle"] == "Kitchen"
    assert result["fieldSources"]["categoryTitle"] == "custom"


def test_category_title_for_a_locale_the_master_does_not_carry():
    """A locale with no master block falls back to the root category, not blank."""
    custom, master = documents()

    result = resolve_documents(custom, master, "fr_FR")

    assert result["product"]["category"] == "Appliance"
    assert result["product"]["categoryTitle"] == "Appliance"


def test_match_key_prefers_gtin_and_normalizes_make_model_fallback():
    assert master_match_key("Any", "Model", ["2", "1"]) == "gtin:1"
    assert master_match_key("  Bosch ", "ABC   1", []) == "mm:bosch|abc 1"
