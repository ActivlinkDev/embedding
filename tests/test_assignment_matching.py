"""Assignment rule matching: what/when, specificity and ordering."""

import pytest

from utils import category_tree
from routers import product_assignment as pa

TAXONOMY = {
    "Washer Dryer": {"category": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances"},
    "Kettle": {"category": "Kettle", "group": "Kitchen", "sector": "Small Domestic Appliances"},
    "LED Television": {"category": "LED Television", "group": "Entertainment", "sector": "Technology"},
}


@pytest.fixture(autouse=True)
def taxonomy(monkeypatch):
    from conftest import seed_taxonomy
    seed_taxonomy(monkeypatch, TAXONOMY)


def req(**over):
    base = dict(client="AO", source="POS", category="Washer Dryer", price=400.0,
                locale="en_GB", purchase_date="2025-01-01", gtee=12, currency="GBP")
    base.update(over)
    return pa.ProductAssignmentRequest(**base)


def rule(**over):
    base = {
        "_id": "aaa", "ruleId": "R", "status": "active", "priority": 100,
        "validFrom": None, "validTo": None,
        "who": [{"client": "AO", "source": "POS"}],
        "what": {"sector": [], "group": [], "category": ["Washer Dryer"]},
        "when": {"locale": ["en_GB"], "currency": ["GBP"], "guaranteeMonths": [12, 24],
                 "deviceAgeMonths": {"min": 0, "max": 12}, "price": {"min": 0, "max": 999.99}},
        "then": {"products": [{"productId": "SDA", "mode": "payment", "terms": [12]}]},
    }
    for k, v in over.items():
        base[k] = v
    return base


# ---- what: the three levels ----

@pytest.mark.parametrize("what,expected", [
    ({"sector": [], "group": [], "category": ["Washer Dryer"]}, 3),
    ({"sector": [], "group": ["Laundry"], "category": []}, 2),
    ({"sector": ["Home Appliances"], "group": [], "category": []}, 1),
])
def test_each_level_matches_and_scores(what, expected):
    placement = category_tree.resolve("Washer Dryer")
    assert pa._what_match(rule(what=what), placement) == expected


def test_multi_level_hit_scores_as_the_most_precise():
    """A rule naming both the sector and the category scores as the category."""
    placement = category_tree.resolve("Washer Dryer")
    what = {"sector": ["Home Appliances"], "group": ["Laundry"], "category": ["Washer Dryer"]}
    assert pa._what_match(rule(what=what), placement) == 3


def test_no_level_hits():
    placement = category_tree.resolve("Kettle")
    what = {"sector": ["Technology"], "group": [], "category": ["Washer Dryer"]}
    assert pa._what_match(rule(what=what), placement) is None


def test_sector_rule_covers_a_category_it_never_names():
    """The whole point of the taxonomy: add a category, existing rules cover it."""
    placement = category_tree.resolve("LED Television")
    what = {"sector": ["Technology"], "group": [], "category": []}
    assert pa._what_match(rule(what=what), placement) == 1


def test_kettle_is_not_in_the_home_appliances_sector():
    """Kettle sits under Small Domestic Appliances, so a Home Appliances rule misses it."""
    placement = category_tree.resolve("Kettle")
    what = {"sector": ["Home Appliances"], "group": [], "category": []}
    assert pa._what_match(rule(what=what), placement) is None


def test_all_levels_empty_places_no_restriction_and_warns(caplog):
    placement = category_tree.resolve("Kettle")
    what = {"sector": [], "group": [], "category": []}
    with caplog.at_level("WARNING"):
        assert pa._what_match(rule(what=what), placement) == 1
    assert "no category, group or sector" in caplog.text


# ---- when ----

def test_when_accepts_a_matching_device():
    assert pa.when_failure_reasons(rule(), req(), 6) == []


@pytest.mark.parametrize("over,age,fragment", [
    (dict(locale="fr_FR"), 6, "locale"),
    (dict(currency="EUR"), 6, "currency"),
    (dict(gtee=36), 6, "gtee"),
    ({}, 13, "age_in_months"),
    (dict(price=1500.0), 6, "price"),
])
def test_when_rejects_and_says_why(over, age, fragment):
    reasons = pa.when_failure_reasons(rule(), req(**over), age)
    assert any(fragment in r for r in reasons), reasons


@pytest.mark.parametrize("age", [0, 12])
def test_age_band_is_inclusive_at_both_ends(age):
    assert pa.when_failure_reasons(rule(), req(), age) == []


@pytest.mark.parametrize("price", [0.0, 999.99])
def test_price_band_is_inclusive_at_both_ends(price):
    assert pa.when_failure_reasons(rule(), req(price=price), 6) == []


def test_empty_list_means_any():
    r = rule(when={"locale": [], "currency": [], "guaranteeMonths": [],
                   "deviceAgeMonths": {"min": 0, "max": 120}, "price": {"min": 0, "max": 9999}})
    assert pa.when_failure_reasons(r, req(locale="tr_TR", currency="EUR", gtee=99), 6) == []


# ---- validity window ----

@pytest.mark.parametrize("vf,vt,expected", [
    (None, None, True),
    ("2020-01-01", None, True),
    ("2999-01-01", None, False),
    (None, "2999-01-01", True),
    (None, "2020-01-01", False),
])
def test_validity_window(vf, vt, expected):
    assert pa._is_in_effect(rule(validFrom=vf, validTo=vt)) is expected


# ---- ordering ----

def pick(rules, **over):
    """Run the matcher over a fixed rule set."""
    import unittest.mock as m
    with m.patch.object(pa, "_candidate_rules", return_value=rules):
        won, _ = pa.find_matching_rule(req(**over), 6)
    return won["ruleId"] if won else None


def test_higher_priority_wins():
    lo = rule(_id="a", ruleId="LOW", priority=100)
    hi = rule(_id="b", ruleId="HIGH", priority=200)
    assert pick([lo, hi]) == "HIGH"
    assert pick([hi, lo]) == "HIGH"


def test_equal_priority_most_specific_wins():
    """A sector-wide rule and a category exception: the exception wins."""
    broad = rule(_id="a", ruleId="SECTOR", priority=100,
                 what={"sector": ["Home Appliances"], "group": [], "category": []})
    narrow = rule(_id="b", ruleId="CATEGORY", priority=100,
                  what={"sector": [], "group": [], "category": ["Washer Dryer"]})
    assert pick([broad, narrow]) == "CATEGORY"
    assert pick([narrow, broad]) == "CATEGORY"


def test_priority_beats_specificity():
    broad = rule(_id="a", ruleId="SECTOR", priority=500,
                 what={"sector": ["Home Appliances"], "group": [], "category": []})
    narrow = rule(_id="b", ruleId="CATEGORY", priority=100)
    assert pick([broad, narrow]) == "SECTOR"


def test_full_tie_is_stable_by_id_not_insertion_order():
    a = rule(_id="aaa", ruleId="A")
    b = rule(_id="bbb", ruleId="B")
    assert pick([a, b]) == pick([b, a]) == "A"


def test_expired_rule_is_not_selected():
    live = rule(_id="a", ruleId="LIVE", priority=100)
    dead = rule(_id="b", ruleId="DEAD", priority=999, validTo="2020-01-01")
    assert pick([live, dead]) == "LIVE"


def test_no_match_reports_every_rejection():
    import unittest.mock as m
    with m.patch.object(pa, "_candidate_rules", return_value=[rule(ruleId="R1"), rule(ruleId="R2")]):
        won, rejected = pa.find_matching_rule(req(price=99999.0), 6)
    assert won is None
    assert {r["rule_id"] for r in rejected} == {"R1", "R2"}
    assert all(r["failure_reasons"] for r in rejected)
