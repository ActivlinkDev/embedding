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
        "category": "Dishwasher",
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


def test_rating_reads_the_root_category_and_display_reads_the_locale_block():
    """The locale block holds the translation; only the root feeds rating."""
    custom, master = documents()
    master["locales"]["es_ES"] = {"title": "T\u00edtulo", "category": "Lavavajillas"}
    custom["enabledLocales"] = ["en_GB", "es_ES"]

    result = resolve_documents(custom, master, "es_ES")

    assert result["product"]["category"] == "Dishwasher"
    assert result["product"]["categoryTitle"] == "Lavavajillas"


def test_a_translated_locale_category_never_reaches_the_rating_key():
    """The regression this split exists to prevent: mis-rating a Spanish journey."""
    custom, master = documents()
    for locale, title in (("es_ES", "Lavavajillas"), ("fr_FR", "Lave-vaisselle")):
        master["locales"][locale] = {"category": title}
        assert resolve_documents(custom, master, locale)["product"]["category"] == "Dishwasher"


def test_a_locale_block_naming_a_different_category_does_not_win_over_the_root():
    """Older masters can disagree — a locale added after a reclassification. The
    root is authoritative for rating now, and the locale block only for display."""
    custom, master = documents()
    master["category"] = "Appliance"

    result = resolve_documents(custom, master, "en_GB")

    assert result["product"]["category"] == "Appliance"
    assert result["product"]["categoryTitle"] == "Dishwasher"


def test_category_title_falls_back_to_the_category_before_the_backfill():
    """A locale block still holding the untranslated category renders the same."""
    custom, master = documents()
    master["locales"]["it_IT"] = {"title": "Titolo"}

    result = resolve_documents(custom, master, "it_IT")

    assert result["product"]["categoryTitle"] == "Dishwasher"


def test_a_category_override_names_its_own_title():
    """A tenant that renames the category has named what the card renders too."""
    custom, master = documents()
    custom["overrides"] = {"locales": {"en_GB": {"category": "Kitchen"}}}

    result = resolve_documents(custom, master, "en_GB")

    assert result["product"]["category"] == "Kitchen"
    assert result["product"]["categoryTitle"] == "Kitchen"
    assert result["fieldSources"]["categoryTitle"] == "custom"


def test_a_root_override_sets_the_rating_category_for_every_locale():
    custom, master = documents()
    master["locales"]["es_ES"] = {"category": "Lavavajillas"}
    custom["overrides"] = {"category": "Kitchen"}

    result = resolve_documents(custom, master, "es_ES")

    assert result["product"]["category"] == "Kitchen"


def test_category_for_a_locale_the_master_does_not_carry():
    """A locale with no master block still rates, and renders the root category."""
    custom, master = documents()

    result = resolve_documents(custom, master, "fr_FR")

    assert result["product"]["category"] == "Dishwasher"
    assert result["product"]["categoryTitle"] == "Dishwasher"


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
