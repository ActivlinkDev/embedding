"""Carrying a repair booking from the basket line to the issued contract.

The repair journey books an appointment on a service request, then adds the chosen option
to the basket. Contract issuance reads the basket line, so the service request and its
appointment have to survive that hop — and everything here has to stay invisible to the
ordinary cover journey, which carries neither.
"""

from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from fastapi import HTTPException

from routers import basket as basket_route
from routers.basket import AddToBasketRequest
from routers.basket.payment import _extract_service_request_ids

SERVICE_REQUEST_ID = "68b2d1f0a4b21d0f8c9e8801"
APPOINTMENT = "2026-09-16"


@pytest.fixture
def basket_db(monkeypatch):
    """A quote with one rateable option, and mocked collections."""
    quote_id = ObjectId()
    device_id = str(ObjectId())
    quotes = MagicMock()
    quotes.find_one.return_value = {
        "_id": quote_id,
        "deviceId": device_id,
        "locale": "en_GB",
        "make": "Bosch",
        "model": "SMS6ZCI00G",
        "responses": [{
            "product_id": "EW1", "category": "Dishwasher", "currency": "GBP", "lang": "en",
            "options": [{"poc": "P", "mode": "payment", "rate": 1, "rounded_price": 99.0,
                         "rounded_price_pence": 9900}],
        }],
    }
    baskets = MagicMock()
    basket_id = ObjectId()
    baskets.insert_one.return_value.inserted_id = basket_id
    # The route reads the basket back after inserting it; hand back what it just wrote.
    baskets.find_one.side_effect = lambda *a, **k: (
        {"_id": basket_id, **baskets.insert_one.call_args.args[0]}
        if baskets.insert_one.call_args else None)
    devices = MagicMock()
    devices.find_one.return_value = None
    monkeypatch.setattr(basket_route, "quotes_collection", quotes)
    monkeypatch.setattr(basket_route, "basket_collection", baskets)
    monkeypatch.setattr(basket_route, "devices_collection", devices)
    # Re-rating is a separate concern and hits its own collections; these tests only
    # care what lands on the line.
    monkeypatch.setattr(basket_route, "rate_basket", lambda *a, **k: MagicMock(
        subtotal=9900, final_total=9900, best=None))
    return {"quote_id": str(quote_id), "device_id": device_id, "baskets": baskets}


def purchase(basket_db, **extra):
    return AddToBasketRequest(
        quote_id=basket_db["quote_id"], product_id="EW1", optionref=0, **extra)


def stored_line(basket_db):
    """The purchase line as it was written to the new basket document."""
    return basket_db["baskets"].insert_one.call_args.args[0]["Basket"][0]


def test_a_repair_line_carries_its_booking(basket_db):
    basket_route.add_to_basket(purchase(
        basket_db, service_request_id=SERVICE_REQUEST_ID, appointment_date=APPOINTMENT))
    line = stored_line(basket_db)
    assert line["service_request_id"] == SERVICE_REQUEST_ID
    assert line["appointment_date"] == APPOINTMENT


def test_an_ordinary_cover_line_is_unchanged(basket_db):
    # The fields are absent rather than null, so nothing downstream has to distinguish
    # "no repair booking" from "a repair booking with no id".
    basket_route.add_to_basket(purchase(basket_db))
    line = stored_line(basket_db)
    assert "service_request_id" not in line
    assert "appointment_date" not in line


def test_a_promo_on_a_purchase_line_no_longer_raises(basket_db):
    # Regression: the promo attach block wrote to `skipped_item`, which is only assigned in
    # the other branch of the if/else — so a purchase line carrying a promo_id raised
    # NameError and returned 500. The offer page sends one whenever a promotion applies.
    basket_route.add_to_basket(purchase(basket_db, promo_id="10YP"))
    assert stored_line(basket_db)["promo_id"] == "10YP"


def test_a_malformed_appointment_date_is_refused_at_the_edge():
    for bad in ("16-09-2026", "2026-9-16", "tomorrow", ""):
        with pytest.raises(ValueError):
            AddToBasketRequest(quote_id="q", product_id="p", optionref=0, appointment_date=bad)


def test_stripe_metadata_lists_each_booking_once():
    ids = _extract_service_request_ids([
        {"service_request_id": "b"}, {"service_request_id": "a"},
        {"service_request_id": "a"}, {"product_id": "no-repair"}])
    assert ids == "a,b"


def test_stripe_metadata_is_empty_for_an_ordinary_basket():
    # Stripe rejects a None metadata value; an all-cover basket must yield a string.
    assert _extract_service_request_ids([{"product_id": "EW1"}, {}]) == ""


def test_stripe_metadata_stays_within_the_value_limit():
    many = [{"service_request_id": str(ObjectId())} for _ in range(100)]
    assert len(_extract_service_request_ids(many)) <= 480
