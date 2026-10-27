"""Checkout session creation.

A link-shortener failure must not fail a checkout (the short link is only an SMS
convenience), and `payment_method_types` is only sent to Stripe when a caller sets
it: Stripe rejects it on accounts with Dashboard-managed payment methods.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routers.generate_payment_link as gpl


def _request():
    return gpl.CheckoutSessionRequest(
        product_name="Cover",
        unit_amount=4899,
        currency="gbp",
        quantity=1,
        mode=gpl.ModeEnum.payment,
        success_url="https://example.com/ok",
        cancel_url="https://example.com/cancel",
        internal_reference="basket-1",
    )


@pytest.fixture
def stripe_session(monkeypatch):
    session = SimpleNamespace(
        url="https://checkout.stripe.com/c/pay/cs_test_1", id="cs_test_1",
        expires_at=1, status="open", customer=None,
    )
    monkeypatch.setattr(gpl.stripe.checkout.Session, "create", lambda **_: session)
    return session


def test_tinyurl_http_error_falls_back_to_the_stripe_url(monkeypatch, stripe_session):
    def fail(_url):
        raise HTTPException(status_code=502, detail="TinyURL error: Unauthorized")
    monkeypatch.setattr(gpl, "shorten_with_tinyurl", fail)

    out = gpl.generate_checkout_session(_request())

    assert out["checkout_url"] == stripe_session.url
    assert out["checkout_url_short"] == stripe_session.url
    assert out["session_id"] == "cs_test_1"


def test_tinyurl_network_error_falls_back_too(monkeypatch, stripe_session):
    def fail(_url):
        raise HTTPException(status_code=500, detail="TinyURL API error: timed out")
    monkeypatch.setattr(gpl, "shorten_with_tinyurl", fail)

    assert gpl.generate_checkout_session(_request())["checkout_url_short"] == stripe_session.url


def test_a_working_shortener_is_still_used(monkeypatch, stripe_session):
    monkeypatch.setattr(gpl, "shorten_with_tinyurl", lambda _url: "https://tinyurl.com/abc")
    assert gpl.generate_checkout_session(_request())["checkout_url_short"] == "https://tinyurl.com/abc"


def test_a_stripe_rejection_still_fails(monkeypatch):
    def reject(**_):
        raise gpl.stripe.error.InvalidRequestError("No such price", param="currency")
    monkeypatch.setattr(gpl.stripe.checkout.Session, "create", reject)
    with pytest.raises(HTTPException) as exc:
        gpl.generate_checkout_session(_request())
    assert exc.value.status_code == 400


def test_payment_method_types_is_not_sent_by_default(monkeypatch):
    # Stripe now rejects `payment_method_types` on accounts using Dashboard-managed
    # (dynamic) payment methods, which failed every checkout. Leave it out unless asked.
    seen = {}

    def create(**params):
        seen.update(params)
        return SimpleNamespace(url="https://checkout.stripe.com/c/pay/cs_test_2", id="cs_test_2",
                               expires_at=1, status="open", customer=None)
    monkeypatch.setattr(gpl.stripe.checkout.Session, "create", create)
    monkeypatch.setattr(gpl, "shorten_with_tinyurl", lambda _url: "https://tinyurl.com/x")

    gpl.generate_checkout_session(_request())
    assert "payment_method_types" not in seen


def test_payment_method_types_is_sent_when_a_caller_sets_it(monkeypatch):
    seen = {}

    def create(**params):
        seen.update(params)
        return SimpleNamespace(url="https://checkout.stripe.com/c/pay/cs_test_3", id="cs_test_3",
                               expires_at=1, status="open", customer=None)
    monkeypatch.setattr(gpl.stripe.checkout.Session, "create", create)
    monkeypatch.setattr(gpl, "shorten_with_tinyurl", lambda _url: "https://tinyurl.com/x")

    req = _request()
    req.payment_method_types = ["card"]
    gpl.generate_checkout_session(req)
    assert seen["payment_method_types"] == ["card"]
