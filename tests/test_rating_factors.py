"""Rating factor lookups: bands and the three-level category match."""

import pytest

from utils import category_tree
from routers import rate_request as rr

TAXONOMY = {
    "Washer Dryer": {"category": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances"},
    "Kettle": {"category": "Kettle", "group": "Kitchen", "sector": "Small Domestic Appliances"},
    "Oven": {"category": "Oven", "group": "Kitchen", "sector": "Home Appliances"},
}


@pytest.fixture(autouse=True)
def taxonomy(monkeypatch):
    from conftest import seed_taxonomy
    seed_taxonomy(monkeypatch, TAXONOMY)


# ---- bands ----

# The live ageFactor, as bands: 25 month-keys collapsed to four rows.
AGE = [
    {"minMonths": 0, "maxMonths": 0, "factor": 1},
    {"minMonths": 1, "maxMonths": 5, "factor": 1.1},
    {"minMonths": 6, "maxMonths": 10, "factor": 1.2},
    {"minMonths": 11, "maxMonths": 24, "factor": 2},
]


@pytest.mark.parametrize("age,expected", [
    (0, 1), (1, 1.1), (5, 1.1), (6, 1.2), (10, 1.2), (11, 2), (24, 2),
])
def test_age_bands_including_every_boundary(age, expected):
    assert rr.find_band(AGE, age, "minMonths", "maxMonths") == expected


@pytest.mark.parametrize("age", [25, 60, 120])
def test_age_above_the_top_band_has_no_factor(age):
    """The live gap: rules admit devices to 120 months, the table stops at 24."""
    assert rr.find_band(AGE, age, "minMonths", "maxMonths") is None


def test_bands_expand_back_to_the_original_month_keys():
    """The conversion must be lossless: 25 keys in, 25 keys out, same values."""
    expanded = {}
    for band in AGE:
        for m in range(band["minMonths"], band["maxMonths"] + 1):
            expanded[m] = band["factor"]
    original = {0: 1, **{i: 1.1 for i in range(1, 6)},
                **{i: 1.2 for i in range(6, 11)}, **{i: 2 for i in range(11, 25)}}
    assert expanded == original
    assert len(expanded) == 25


def test_a_gap_between_bands_has_no_factor():
    gapped = [{"min": 0, "max": 10, "factor": 1}, {"min": 20, "max": 30, "factor": 2}]
    assert rr.find_band(gapped, 15) is None
    assert rr.find_band(gapped, 10) == 1
    assert rr.find_band(gapped, 20) == 2


def test_malformed_band_is_skipped_not_crashed():
    assert rr.find_band([{"factor": 9}, {"min": 0, "max": 5, "factor": 1}], 3) == 1


@pytest.mark.parametrize("bands", [None, []])
def test_missing_bands(bands):
    assert rr.find_band(bands, 1) is None


# ---- price bands ----

PRICE = [
    {"min": 0, "max": 19.99, "factor": 0.2},
    {"min": 20, "max": 149.99, "factor": 0.5},
    {"min": 150, "max": 499.99, "factor": 1},
    {"min": 500, "max": 1499.99, "factor": 1.5},
]


@pytest.mark.parametrize("price,expected", [
    (0, 0.2), (19.99, 0.2), (20, 0.5), (149.99, 0.5), (150, 1), (499.99, 1), (500, 1.5), (1499.99, 1.5),
])
def test_price_bands(price, expected):
    assert rr.find_price_factor(PRICE, price) == expected


def test_price_above_the_top_band_has_no_factor():
    """Rules admit prices to 9999; this table stops at 1499.99."""
    assert rr.find_price_factor(PRICE, 1500) is None


def test_price_bracket_returns_the_containing_band():
    assert rr.find_price_bracket(PRICE, 400) == (150, 499.99)
    assert rr.find_price_bracket(PRICE, 99999) is None


# ---- category: three levels, most specific wins ----

def test_category_row_matches():
    rows = [{"level": "category", "value": "Kettle", "factor": 0.75}]
    assert rr.find_category_factor(rows, "Kettle") == 0.75


def test_sector_row_covers_a_category_it_never_names():
    rows = [{"level": "sector", "value": "Home Appliances", "factor": 1}]
    assert rr.find_category_factor(rows, "Washer Dryer") == 1


def test_group_row_covers_its_categories():
    rows = [{"level": "group", "value": "Kitchen", "factor": 1.3}]
    assert rr.find_category_factor(rows, "Oven") == 1.3


def test_most_specific_row_wins_regardless_of_order():
    """A sector default plus a category exception — the exception must win."""
    rows = [
        {"level": "sector", "value": "Home Appliances", "factor": 1},
        {"level": "group", "value": "Kitchen", "factor": 1.1},
        {"level": "category", "value": "Oven", "factor": 0.5},
    ]
    assert rr.find_category_factor(rows, "Oven") == 0.5
    assert rr.find_category_factor(list(reversed(rows)), "Oven") == 0.5


def test_group_beats_sector_when_no_category_row():
    rows = [
        {"level": "sector", "value": "Home Appliances", "factor": 1},
        {"level": "group", "value": "Kitchen", "factor": 1.1},
    ]
    assert rr.find_category_factor(rows, "Oven") == 1.1


def test_sibling_exception_does_not_leak():
    rows = [
        {"level": "sector", "value": "Home Appliances", "factor": 1},
        {"level": "category", "value": "Oven", "factor": 0.5},
    ]
    assert rr.find_category_factor(rows, "Washer Dryer") == 1


def test_unmatched_category_has_no_factor():
    """The live gap: OLED Television is eligible everywhere and priced nowhere."""
    rows = [{"level": "category", "value": "Washing Machine", "factor": 1}]
    assert rr.find_category_factor(rows, "OLED Television") is None


def test_unknown_level_is_ignored():
    rows = [{"level": "brand", "value": "Washer Dryer", "factor": 9},
            {"level": "category", "value": "Washer Dryer", "factor": 1}]
    assert rr.find_category_factor(rows, "Washer Dryer") == 1
