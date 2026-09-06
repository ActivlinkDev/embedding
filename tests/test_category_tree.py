"""The Category taxonomy resolver."""

import pytest

from utils import category_tree


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    monkeypatch.setattr(category_tree, "_cache", {}, raising=False)
    monkeypatch.setattr(category_tree, "_cache_loaded_at", 0.0, raising=False)


def seed(monkeypatch, docs):
    """Seed the cache the way ``_load`` builds it: keyed by the folded name."""
    folded = {category_tree._key(name): dict(entry) for name, entry in docs.items()}
    monkeypatch.setattr(category_tree, "_load", lambda: dict(folded))


TAXONOMY = {
    "Washer Dryer": {"category": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances"},
    "Kettle": {"category": "Kettle", "group": "Kitchen", "sector": "Small Domestic Appliances"},
    "LED Television": {
        "category": "LED Television",
        "group": "Entertainment",
        "sector": "Technology",
        "titles": {
            "en_GB": "LED Television",
            "es_ES": "Televisor LED",
            "fr_FR": "T\u00e9l\u00e9viseur LED",
            "de_DE": "LED-Fernseher",
        },
    },
}


def test_resolves_all_three_levels(monkeypatch):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.resolve("Washer Dryer") == {
        "category": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances",
    }


def test_unknown_category_keeps_its_name_without_a_placement(monkeypatch):
    """A free-typed or 'Unknown' category still matches category-level rules."""
    seed(monkeypatch, TAXONOMY)
    assert category_tree.resolve("Nonesuch") == {
        "category": "Nonesuch", "group": None, "sector": None,
    }


@pytest.mark.parametrize("value", ["", None, "   "])
def test_blank_category(monkeypatch, value):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.resolve(value)["group"] is None


def test_duplicate_category_keeps_the_first(monkeypatch, caplog):
    """`Hob` is duplicated in the live collection."""
    calls = {"n": 0}

    def fake_find(_filter, _projection):
        calls["n"] += 1
        return [
            {"category": "Hob", "group": "Kitchen", "sector": "Home Appliances"},
            {"category": "Hob", "group": "Other", "sector": "Elsewhere"},
        ]

    monkeypatch.setattr(category_tree, "_collection", lambda: type("C", (), {"find": staticmethod(fake_find)})())
    with caplog.at_level("WARNING"):
        assert category_tree.resolve("Hob")["group"] == "Kitchen"
    assert "duplicate category" in caplog.text


def test_result_is_a_copy_callers_cannot_corrupt_the_cache(monkeypatch):
    seed(monkeypatch, TAXONOMY)
    category_tree.resolve("Kettle")["group"] = "TAMPERED"
    assert category_tree.resolve("Kettle")["group"] == "Kitchen"


def test_failed_reload_serves_the_previous_cache(monkeypatch):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.resolve("Kettle")["sector"] == "Small Domestic Appliances"

    def boom():
        raise RuntimeError("mongo down")

    monkeypatch.setattr(category_tree, "_load", boom)
    monkeypatch.setattr(category_tree, "_cache_loaded_at", 0.0, raising=False)
    assert category_tree.resolve("Kettle")["sector"] == "Small Domestic Appliances"


def test_empty_reload_does_not_wipe_the_cache(monkeypatch):
    seed(monkeypatch, TAXONOMY)
    category_tree.resolve("Kettle")
    monkeypatch.setattr(category_tree, "_load", dict)
    monkeypatch.setattr(category_tree, "_cache_loaded_at", 0.0, raising=False)
    assert category_tree.resolve("Kettle")["group"] == "Kitchen"


@pytest.mark.parametrize("written", [
    "Washer Dryer", "washer dryer", "WASHER DRYER", "washer-dryer",
    "Washer/Dryer", "  Washer  Dryer  ",
])
def test_lookup_folds_case_and_punctuation(monkeypatch, written):
    """Categories are free-typed in the admin UI, so spelling drifts."""
    seed(monkeypatch, TAXONOMY)
    assert category_tree.resolve(written)["group"] == "Laundry"


def test_resolve_returns_the_taxonomy_spelling_not_the_caller_s(monkeypatch):
    """Callers compare `category` against rule lists with plain equality."""
    seed(monkeypatch, TAXONOMY)
    assert category_tree.resolve("washer-dryer")["category"] == "Washer Dryer"


# ── Locale titles ─────────────────────────────────────────────────────────────


def test_localized_title_returns_the_translation(monkeypatch):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.localized_title("LED Television", "es_ES") == "Televisor LED"


def test_localized_title_falls_back_to_the_same_language(monkeypatch):
    """`fr_BE` has no title of its own, but French copy still beats English."""
    seed(monkeypatch, TAXONOMY)
    assert category_tree.localized_title("LED Television", "fr_BE") == "T\u00e9l\u00e9viseur LED"


def test_localized_title_falls_back_to_english_not_blank(monkeypatch):
    """A locale the taxonomy has no title for must not blank the product card."""
    seed(monkeypatch, TAXONOMY)
    assert category_tree.localized_title("LED Television", "it_IT") == "LED Television"


def test_localized_title_of_a_category_with_no_titles(monkeypatch):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.localized_title("Kettle", "es_ES") == "Kettle"


def test_localized_title_of_an_unknown_category_keeps_the_caller_s_name(monkeypatch):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.localized_title("Nonesuch", "es_ES") == "Nonesuch"


def test_localized_title_uses_the_taxonomy_spelling(monkeypatch):
    """A free-typed category resolves before its title is looked up."""
    seed(monkeypatch, TAXONOMY)
    assert category_tree.localized_title("led-television", "es_ES") == "Televisor LED"


@pytest.mark.parametrize("category", ["", None, "   "])
def test_localized_title_of_a_blank_category(monkeypatch, category):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.localized_title(category, "es_ES") == ""


@pytest.mark.parametrize("locale", ["", None, "  "])
def test_localized_title_without_a_locale_reads_in_english(monkeypatch, locale):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.localized_title("LED Television", locale) == "LED Television"


def test_titles_are_read_from_the_collection_shape(monkeypatch):
    """`locale_title` is a list of {locale, title}; half-filled rows are dropped."""

    def fake_find(_filter, _projection):
        return [{
            "category": "LED Television",
            "group": "Entertainment",
            "sector": "Technology",
            "locale_title": [
                {"locale": "es_ES", "title": "Televisor LED"},
                {"locale": "fr_FR", "title": ""},
                {"locale": "", "title": "orphan"},
                "not-a-dict",
            ],
        }]

    monkeypatch.setattr(
        category_tree, "_collection", lambda: type("C", (), {"find": staticmethod(fake_find)})()
    )
    assert category_tree.localized_title("LED Television", "es_ES") == "Televisor LED"
    assert category_tree.localized_title("LED Television", "fr_FR") == "LED Television"


def test_resolve_never_leaks_titles_into_the_placement_contract(monkeypatch):
    """Callers compare the whole dict; an extra key would break them."""
    seed(monkeypatch, TAXONOMY)
    assert category_tree.resolve("LED Television") == {
        "category": "LED Television", "group": "Entertainment", "sector": "Technology",
    }


# ── Membership ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("written", ["Washer Dryer", "washer-dryer", "  WASHER  DRYER "])
def test_known_folds_case_and_punctuation(monkeypatch, written):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.known(written) is True


@pytest.mark.parametrize("written", ["Nonesuch", "", None, "   "])
def test_known_rejects_what_the_taxonomy_does_not_hold(monkeypatch, written):
    seed(monkeypatch, TAXONOMY)
    assert category_tree.known(written) is False


def test_known_does_not_infer_membership_from_group_and_sector(monkeypatch):
    """A legitimate top-level entry carries neither and is still a real category."""
    seed(monkeypatch, {"Orphan": {"category": "Orphan", "group": None, "sector": None}})
    assert category_tree.known("Orphan") is True
    assert category_tree.resolve("Orphan")["group"] is None


def test_loaded_reports_an_unreadable_taxonomy(monkeypatch):
    """`known` returning False must be distinguishable from Mongo being down."""
    monkeypatch.setattr(category_tree, "_load", dict)
    assert category_tree.loaded() is False
    assert category_tree.known("Washer Dryer") is False

    seed(monkeypatch, TAXONOMY)
    assert category_tree.loaded() is True
