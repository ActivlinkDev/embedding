"""Finding the Stripe Customer that pre-fills the phone on Checkout.

Stripe returns StripeObjects, which current stripe-python no longer treats as dicts, so
these tests hand back real ones rather than plain dicts that would hide a `.get` call.
"""

from types import SimpleNamespace

import stripe

from routers import generate_payment_link as gpl


def _customer(**fields):
    return stripe.Customer.construct_from(fields, "sk_test_dummy")


def test_an_existing_customer_with_a_new_phone_is_updated_and_reused(monkeypatch):
    found = _customer(id="cus_1", email="jane@example.com", phone=None)
    modified = {}
    monkeypatch.setattr(gpl.stripe.Customer, "list", lambda **_: SimpleNamespace(data=[found]))

    def modify(cid, **updates):
        modified.update(updates, id=cid)
        return _customer(id=cid, email="jane@example.com", phone=updates["phone"])

    monkeypatch.setattr(gpl.stripe.Customer, "modify", modify)
    assert gpl._resolve_stripe_customer_id("jane@example.com", "+447700900123") == "cus_1"
    assert modified == {"id": "cus_1", "phone": "+447700900123"}


def test_an_unchanged_customer_is_reused_without_an_update(monkeypatch):
    found = _customer(id="cus_2", email="jane@example.com", phone="+447700900123")
    monkeypatch.setattr(gpl.stripe.Customer, "list", lambda **_: SimpleNamespace(data=[found]))

    def never(*_a, **_k):
        raise AssertionError("nothing to update")

    monkeypatch.setattr(gpl.stripe.Customer, "modify", never)
    assert gpl._resolve_stripe_customer_id("jane@example.com", "+447700900123") == "cus_2"


def test_a_new_customer_is_created_when_none_matches(monkeypatch):
    monkeypatch.setattr(gpl.stripe.Customer, "list", lambda **_: SimpleNamespace(data=[]))
    monkeypatch.setattr(gpl.stripe.Customer, "search", lambda **_: SimpleNamespace(data=[]))
    monkeypatch.setattr(
        gpl.stripe.Customer, "create",
        lambda **params: _customer(id="cus_3", **params),
    )
    assert gpl._resolve_stripe_customer_id("jane@example.com", "+447700900123") == "cus_3"
