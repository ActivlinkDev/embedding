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
    "LED Television": {"category": "LED Television", "group": "Entertainment", "sector": "Technology"},
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
