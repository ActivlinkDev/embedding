"""The checkout channel is recorded on the Stripe session.

An Apple Pay express purchase skips phone validation, so the session metadata says
which way the customer came in. The channel is a label only: the amount must still
come from the stored basket.
"""

import pytest
from bson import ObjectId
from pydantic import ValidationError

from routers.basket import payment

BASKET_ID = ObjectId()


class FakeCollection:
    def find_one(self, query):
        assert query == {"_id": BASKET_ID}
        return {
            "_id": BASKET_ID,
            "final_total": 4999,
            "Basket": [{"mode": "payment", "currency": "GBP", "client": "acme", "rounded_price_pence": 4999}],
        }


@pytest.fixture
def captured(monkeypatch):
    seen = {}
    monkeypatch.setattr(payment, "basket_collection", FakeCollection())
    monkeypatch.setattr(payment, "generate_checkout_session", lambda req: seen.setdefault("req", req) and {"checkout_url": "u"})
    return seen


def create(**extra):
    return payment.create_basket_payment_session(
        payment.BasketPaymentRequest(basket_id=str(BASKET_ID), success_url="https://s", cancel_url="https://c", **extra),
        None,
    )


def test_apple_pay_channel_reaches_stripe_metadata(captured):
    create(checkout_channel="apple_pay")
    assert captured["req"].metadata["checkout_channel"] == "apple_pay"


def test_channel_defaults_to_basket(captured):
    create()
    assert captured["req"].metadata["checkout_channel"] == "basket"


def test_channel_does_not_affect_the_amount(captured):
    create(checkout_channel="apple_pay")
    assert captured["req"].unit_amount == 4999


def test_unknown_channel_is_rejected():
    with pytest.raises(ValidationError):
        payment.BasketPaymentRequest(basket_id=str(BASKET_ID), checkout_channel="bitcoin")
