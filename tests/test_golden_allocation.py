"""Assignment and rating together, on the shapes actually loaded in Mongo.

These mirror the live documents closely enough to pin real behaviour — including
the quirks we deliberately preserved during the conversion — without committing
the full rate card. Values that matter to an assertion are the live ones.
"""

import unittest.mock as mock

import pytest

from utils import category_tree
from routers import product_assignment as pa
from routers import rate_request as rr

TAXONOMY = {
    "Washing Machine": {"category": "Washing Machine", "group": "Laundry", "sector": "Home Appliances"},
    "OLED Television": {"category": "OLED Television", "group": "Entertainment", "sector": "Technology"},
    "Kettle": {"category": "Kettle", "group": "Kitchen", "sector": "Small Domestic Appliances"},
}
CATEGORIES = sorted(TAXONOMY)


@pytest.fixture(autouse=True)
def taxonomy(monkeypatch):
    from conftest import seed_taxonomy
    seed_taxonomy(monkeypatch, TAXONOMY)


POS_CLIENTS = [{"client": c, "source": "POS"} for c in
               ("AO", "ArgosUK", "Beko", "Bosch", "Hisense", "Media", "SharkNinja")]

# GBP-POS-EX1-WF1-150PLUS (priority 200) and GBP-POS-SDA-UNDER1000 (priority 100).
# Their price bands overlap between 150 and 999.99; priority decides, which is
# how the original criteria array behaved by position.
GBP_POS_HIGH = {
    "_id": "681bd53fad4ba559bc92f41b", "ruleId": "GBP-POS-EX1-WF1-150PLUS",
    "status": "active", "priority": 200, "validFrom": None, "validTo": None,
    "who": POS_CLIENTS,
    "what": {"sector": [], "group": [], "category": CATEGORIES},
    "when": {"locale": ["en_GB"], "currency": ["GBP"], "guaranteeMonths": [12, 24],
             "deviceAgeMonths": {"min": 0, "max": 12}, "price": {"min": 150, "max": 9999}},
    "then": {"products": [{"productId": "EX1", "mode": "payment", "terms": [12, 24, 36]},
                          {"productId": "WF1", "mode": "subscription", "terms": [1]}]},
}
GBP_POS_LOW = {
    "_id": "gbp-pos-sda", "ruleId": "GBP-POS-SDA-UNDER1000",
    "status": "active", "priority": 100, "validFrom": None, "validTo": None,
    "who": POS_CLIENTS,
    "what": {"sector": [], "group": [], "category": CATEGORIES},
    "when": {"locale": ["en_GB"], "currency": ["GBP"], "guaranteeMonths": [12, 24],
             "deviceAgeMonths": {"min": 0, "max": 12}, "price": {"min": 0, "max": 999.99}},
    "then": {"products": [{"productId": "SDA", "mode": "payment", "terms": [12]}]},
}
GBP_PON = {
    "_id": "6903f69a5238db5abd2b7f47", "ruleId": "GBP-PON-SUBSCRIPTION",
    "status": "active", "priority": 100, "validFrom": None, "validTo": None,
    "who": [{"client": "AO", "source": "PON"}],
    "what": {"sector": [], "group": [], "category": CATEGORIES},
    "when": {"locale": ["en_GB"], "currency": ["GBP"], "guaranteeMonths": [12, 24],
             "deviceAgeMonths": {"min": 0, "max": 120}, "price": {"min": 0, "max": 2500}},
    "then": {"products": [{"productId": "PON", "mode": "subscription", "terms": [1]}]},
}
ALL_RULES = [GBP_POS_HIGH, GBP_POS_LOW, GBP_PON]

# GBP-PON-SUBSCRIPTION rating table.
PON_RATING = {
    "ruleId": "GBP-PON-SUBSCRIPTION", "status": "active",
    "who": [{"client": "AO", "source": "PON"}],
    "products": ["PON"], "currency": "GBP", "baseFee": 9.99,
    "localeFactor": [{"locale": "en_GB", "factor": 1}],
    "pocFactor": {"1": 1},
    "categoryFactor": [
        {"level": "category", "value": "Washing Machine", "factor": 0.8},
        {"level": "category", "value": "Kettle", "factor": 0.75},
    ],
    "ageFactor": [{"minMonths": 0, "maxMonths": 24, "factor": 1}],
    "priceFactor": [{"min": 0, "max": 2500, "factor": 1}],
    "multiFactor": [{"min": 1, "max": 1, "factor": 1}],
}


def assign(client, source, category, price, age_months=6, **over):
    """Run assignment against the fixed rule set."""
    from datetime import date
    from dateutil_shim import months_ago
    payload = pa.ProductAssignmentRequest(
        client=client, source=source, category=category, price=price,
        locale=over.get("locale", "en_GB"), purchase_date=months_ago(age_months),
        gtee=over.get("gtee", 12), currency=over.get("currency", "GBP"))
    with mock.patch.object(pa, "_candidate_rules",
                           return_value=[r for r in ALL_RULES
                                         if {"client": client, "source": source} in r["who"]]):
        rule, rejected = pa.find_matching_rule(payload, age_months)
    return rule, rejected


# ---- assignment ----

def test_pon_device_gets_the_pon_product():
    rule, _ = assign("AO", "PON", "Washing Machine", 400.0)
    assert rule["ruleId"] == "GBP-PON-SUBSCRIPTION"
    assert rule["then"]["products"] == [
        {"productId": "PON", "mode": "subscription", "terms": [1]}]


def test_pos_device_over_150_gets_ex1_wf1_not_sda():
    """Pins the 999.99 overlap: both rules match, priority 200 wins."""
    rule, _ = assign("Beko", "POS", "Washing Machine", 400.0)
    assert rule["ruleId"] == "GBP-POS-EX1-WF1-150PLUS"
    assert [p["productId"] for p in rule["then"]["products"]] == ["EX1", "WF1"]


def test_pos_device_under_150_gets_sda():
    rule, _ = assign("Beko", "POS", "Washing Machine", 100.0)
    assert rule["ruleId"] == "GBP-POS-SDA-UNDER1000"


def test_newly_eligible_client_matches():
    """SharkNinja was in no rule before all clients were added."""
    rule, _ = assign("SharkNinja", "POS", "Washing Machine", 400.0)
    assert rule["ruleId"] == "GBP-POS-EX1-WF1-150PLUS"


def test_pon_product_is_not_offered_to_a_pos_client():
    """Source keeps the AO-specific PON product off other channels."""
    rule, _ = assign("Beko", "POS", "Washing Machine", 400.0)
    assert "PON" not in [p["productId"] for p in rule["then"]["products"]]


def test_pos_rules_are_not_offered_to_the_pon_channel():
    rule, _ = assign("AO", "PON", "Washing Machine", 400.0)
    assert rule["ruleId"] == "GBP-PON-SUBSCRIPTION"


def test_pon_admits_a_device_older_than_the_rate_table_covers():
    """Eligible to 120 months. Rating stops at 24 — the gap is still open."""
    rule, _ = assign("AO", "PON", "Washing Machine", 400.0, age_months=60)
    assert rule is not None


# ---- rating ----

def rate(category, age, price, product="PON", client="AO", source="PON"):
    req = rr.RateRequest(product_id=product, currency="GBP", locale="en_GB", poc=1,
                         category=category, age=age, price=price, multi_count=1,
                         client=client, source=source, mode="subscription")
    return rr.match_with_reasons(PON_RATING, req), req


def test_pon_prices_a_covered_device():
    (matched, reasons), req = rate("Washing Machine", 6, 400.0)
    assert matched, reasons
    rate_value = round(
        PON_RATING["baseFee"]
        * 1  # locale
        * PON_RATING["pocFactor"]["1"]
        * rr.find_category_factor(PON_RATING["categoryFactor"], req.category)
        * rr.find_band(PON_RATING["ageFactor"], req.age, "minMonths", "maxMonths")
        * rr.find_price_factor(PON_RATING["priceFactor"], req.price)
        * rr.find_band(PON_RATING["multiFactor"], req.multi_count), 2)
    assert rate_value == round(9.99 * 0.8, 2) == 7.99


def test_age_beyond_the_table_fails_closed_with_a_reason():
    """Assignment says yes at 60 months; rating must refuse, not guess."""
    (matched, reasons), _ = rate("Washing Machine", 60, 400.0)
    assert not matched
    assert any("ageFactor" in r for r in reasons), reasons


def test_uncovered_category_fails_closed_with_a_reason():
    (matched, reasons), _ = rate("OLED Television", 6, 400.0)
    assert not matched
    assert any("categoryFactor" in r for r in reasons), reasons


def test_price_beyond_the_table_fails_closed():
    (matched, reasons), _ = rate("Washing Machine", 6, 99999.0)
    assert not matched
    assert any("priceFactor" in r for r in reasons), reasons


def test_wrong_product_is_rejected():
    (matched, reasons), _ = rate("Washing Machine", 6, 400.0, product="EX1")
    assert not matched
    assert any("products" in r for r in reasons), reasons
