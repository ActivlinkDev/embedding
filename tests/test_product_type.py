"""productType: a rule says whether a product is cover (`contract`) or a one-off `repair`.

It travels assignment → rate line → quote → basket line, where the confirmation page
reads it. It never changes the price, and everything written before it existed is cover.
"""

from unittest.mock import MagicMock

import pytest
from bson import ObjectId

from routers import assign_product_by_device_id as by_device
from routers import product_assignment as pa
from routers import rate_request as rr


@pytest.mark.parametrize("value,expected", [
    (None, "contract"), ("repair", "repair"), ("Repair ", "repair"), ("contract", "contract"),
    ("warranty", "contract"), (7, "contract"),
])
def test_product_type_is_repair_or_contract(value, expected):
    product = {"productId": "PON"} if value is None else {"productId": "PON", "productType": value}
    assert pa.product_type(product) == expected


def test_assignment_carries_the_product_type_onto_each_term(monkeypatch):
    devices = MagicMock()
    devices.find_one.return_value = {
        "_id": ObjectId(), "client": "AO", "locale": "en_GB", "source": "POS",
        "identifiers": {"category": "Washing Machine", "gteeLabour": "12"},
        "registrationParameters": {"price": 400, "currency": "GBP", "purchaseDate": "2026-11-03"},
    }
    monkeypatch.setattr(by_device, "devices_collection", devices)
    monkeypatch.setattr(by_device, "assign_products", lambda payload: {
        "input": payload.model_dump(), "age_in_months": 0, "regional_pricing": False,
        "products": [
            {"productId": "PON", "mode": "payment", "terms": [1], "productType": "repair"},
            {"productId": "EX1", "mode": "payment", "terms": [12, 24]},
        ]})
    products = by_device.assign_product_for_device(str(ObjectId()))["Products"]
    assert {(p["product_id"], p["poc"]): p["product_type"] for p in products} == {
        ("PON", 1): "repair", ("EX1", 12): "contract", ("EX1", 24): "contract"}


def test_the_quote_keeps_the_product_type_per_product():
    responses = [
        {"product_id": "PON", "product_type": "repair", "poc": 1, "status": "ok"},
        {"product_id": "EX1", "product_type": "contract", "poc": 12, "status": "ok"},
        {"product_id": "OLD", "poc": 12, "status": "ok"},
    ]
    grouped = {g["product_id"]: g["product_type"] for g in rr.group_responses(responses)}
    assert grouped == {"PON": "repair", "EX1": "contract", "OLD": "contract"}


def test_the_rate_line_does_not_count_it_as_missing():
    line = rr.RateRequest(product_id="PON", currency="GBP", locale="en_GB", poc=1, category="Oven",
                          age=0, price=400, multi_count=1, client="AO", source="POS", mode="payment")
    assert line.product_type == "contract"
    assert line.missing_fields() == []


@pytest.fixture
def basket_db(monkeypatch):
    from routers import basket as basket_route
    quotes, baskets, devices = MagicMock(), MagicMock(), MagicMock()
    devices.find_one.return_value = None
    basket_id = ObjectId()
    baskets.insert_one.return_value.inserted_id = basket_id
    baskets.find_one.side_effect = lambda *a, **k: (
        {"_id": basket_id, **baskets.insert_one.call_args.args[0]} if baskets.insert_one.call_args else None)
    monkeypatch.setattr(basket_route, "quotes_collection", quotes)
    monkeypatch.setattr(basket_route, "basket_collection", baskets)
    monkeypatch.setattr(basket_route, "devices_collection", devices)
    monkeypatch.setattr(basket_route, "rate_basket", lambda *a, **k: MagicMock(subtotal=12999, final_total=12999, best=None))
    return basket_route, quotes, baskets


@pytest.mark.parametrize("stored,expected", [("repair", "repair"), (None, "contract")])
def test_the_basket_line_carries_it(basket_db, stored, expected):
    basket_route, quotes, baskets = basket_db
    product = {"product_id": "PON", "currency": "GBP", "options": [
        {"poc": 1, "mode": "payment", "rate": 129.99, "rounded_price": 129.99, "rounded_price_pence": 12999}]}
    if stored:
        product["product_type"] = stored
    quotes.find_one.return_value = {"_id": ObjectId(), "deviceId": "D1", "responses": [product]}
    basket_route.add_to_basket(basket_route.AddToBasketRequest(
        quote_id=str(ObjectId()), product_id="PON", optionref=0))
    assert baskets.insert_one.call_args.args[0]["Basket"][0]["product_type"] == expected
