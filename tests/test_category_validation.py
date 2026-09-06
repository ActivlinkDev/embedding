"""Rejecting a caller-supplied category the taxonomy does not hold."""

import pytest
from fastapi import HTTPException

from conftest import seed_taxonomy
from routers.sku.category_validation import validate_category


TAXONOMY = {
    "Washer Dryer": {"category": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances"},
    "LED Television": {
        "category": "LED Television", "group": "Entertainment", "sector": "Technology",
    },
}


def test_a_known_category_passes(monkeypatch):
    seed_taxonomy(monkeypatch, TAXONOMY)
    assert validate_category("LED Television") == "LED Television"


def test_a_known_category_is_stored_in_the_taxonomy_s_spelling(monkeypatch):
    """Rating compares with plain equality, so the caller's spelling is folded."""
    seed_taxonomy(monkeypatch, TAXONOMY)
    assert validate_category("washer-dryer") == "Washer Dryer"


def test_an_unknown_category_is_rejected(monkeypatch):
    """The failure this exists to prevent: an unratable, untranslatable SKU."""
    seed_taxonomy(monkeypatch, TAXONOMY)
    with pytest.raises(HTTPException) as excinfo:
        validate_category("Smart Telly")
    assert excinfo.value.status_code == 422
    assert "Smart Telly" in excinfo.value.detail


def test_the_rejection_names_the_field_it_came_from(monkeypatch):
    seed_taxonomy(monkeypatch, TAXONOMY)
    with pytest.raises(HTTPException) as excinfo:
        validate_category("Smart Telly", "Locale_Details.Category")
    assert "Locale_Details.Category" in excinfo.value.detail


@pytest.mark.parametrize("value", [None, "", "   "])
def test_null_and_blank_pass_through(monkeypatch, value):
    """An explicit null override means 'suppress the inherited value'."""
    seed_taxonomy(monkeypatch, TAXONOMY)
    assert validate_category(value) == value


def test_an_unreadable_taxonomy_does_not_reject_everything(monkeypatch, caplog):
    """A Mongo blip must not block all SKU creation."""
    monkeypatch.setattr("utils.category_tree._load", dict)
    monkeypatch.setattr("utils.category_tree._cache", {}, raising=False)
    monkeypatch.setattr("utils.category_tree._cache_loaded_at", 0.0, raising=False)
    with caplog.at_level("WARNING"):
        assert validate_category("  Smart Telly  ") == "Smart Telly"
    assert "taxonomy is unreadable" in caplog.text
