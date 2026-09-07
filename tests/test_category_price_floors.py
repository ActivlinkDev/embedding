"""The seed script for Category.min_market_price.

The floors it writes decide which enrichment matches get rejected, so the
planning half is a pure function and tested here. Its two invariants matter
more than the numbers: it must never invent a taxonomy category, and it must
never quietly replace a floor someone has tuned by hand.
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "seed_category_price_floors",
    Path(__file__).resolve().parent.parent / "scripts" / "seed_category_price_floors.py",
)
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def _category(name, floor=None):
    doc = {"category": name}
    if floor is not None:
        doc["min_market_price"] = floor
    return doc


def test_a_known_category_gets_a_floor_in_both_currencies():
    updates, _unknown, _unfloored = seed.plan([_category("Dishwasher")], overwrite=False)
    assert updates == [("Dishwasher", {"GBP": 80.0, "EUR": 92.0})]


def test_a_table_entry_the_taxonomy_lacks_is_reported_not_created():
    """Inventing a category would produce one nothing can rate."""
    updates, unknown, _unfloored = seed.plan([_category("Dishwasher")], overwrite=False)
    assert [name for name, _ in updates] == ["Dishwasher"]
    assert "Kettle" in unknown


def test_a_taxonomy_category_with_no_table_entry_is_reported_not_guessed():
    _updates, _unknown, unfloored = seed.plan([_category("Sous Vide Wand")], overwrite=False)
    assert unfloored == ["Sous Vide Wand"]


def test_an_existing_floor_survives_a_re_run():
    """Re-running the seed must not undo a hand-tuned value."""
    updates, _unknown, _unfloored = seed.plan(
        [_category("Dishwasher", {"GBP": 120})], overwrite=False
    )
    assert updates == []


def test_overwrite_replaces_an_existing_floor():
    updates, _unknown, _unfloored = seed.plan(
        [_category("Dishwasher", {"GBP": 120})], overwrite=True
    )
    assert updates == [("Dishwasher", {"GBP": 80.0, "EUR": 92.0})]


def test_matching_folds_case_and_punctuation_like_the_taxonomy_does():
    updates, _unknown, unfloored = seed.plan([_category("dish-washer")], overwrite=False)
    assert [name for name, _ in updates] == ["dish-washer"]
    assert unfloored == []


def test_a_nameless_document_is_skipped():
    updates, _unknown, unfloored = seed.plan([{"category": ""}, {}], overwrite=False)
    assert updates == [] and unfloored == []


def test_no_floor_is_written_in_a_currency_the_locales_do_not_use():
    """TRL is deliberately absent: a stale rate would reject every match."""
    assert set(seed._floors(80)) == {"GBP", "EUR"}


@pytest.mark.parametrize("name,gbp", sorted(seed.FLOORS_GBP.items()))
def test_every_floor_is_a_positive_number(name, gbp):
    """A zero or negative floor reads as "no floor" and would be a silent no-op."""
    assert isinstance(gbp, (int, float)) and not isinstance(gbp, bool)
    assert gbp > 0


def test_the_table_has_no_duplicate_categories_under_folding():
    """Two spellings of one category would make the last write win at random."""
    folded = [seed._key(name) for name in seed.FLOORS_GBP]
    assert len(folded) == len(set(folded))
