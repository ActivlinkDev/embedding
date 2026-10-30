"""Reading a basket's checkout session back.

The confirmation page asks this before saying "Payment confirmed", because with in-page
payment it is the page, not Stripe, that navigates there. It must not become a way to read
arbitrary Stripe sessions, so only sessions created for a basket are answered.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from routers.basket import payment


def _session(**fields):
    """A real Stripe Session, not a dict: current stripe-python objects have no `.get`."""
    return payment.stripe.checkout.Session.construct_from(fields, "sk_test")


def _retrieve_returning(monkeypatch, session):
    monkeypatch.setattr(payment.stripe.checkout.Session, "retrieve", lambda _id: session)


def test_a_paid_basket_session_reports_paid(monkeypatch):
    _retrieve_returning(monkeypatch, _session(
        id="cs_test_1", status="complete", payment_status="paid",
        metadata={"basket_id": "68b2d1f0a4b21d0f8c9e8801", "client": "acme"}))
    out = payment.get_basket_payment_session("cs_test_1", scope=None)
    assert out == {"session_id": "cs_test_1", "status": "complete", "payment_status": "paid",
                   "basket_id": "68b2d1f0a4b21d0f8c9e8801"}


def test_a_session_not_made_for_a_basket_is_not_found(monkeypatch):
    _retrieve_returning(monkeypatch, _session(
        id="cs_test_2", status="complete", payment_status="paid", metadata={}))
    with pytest.raises(HTTPException) as exc:
        payment.get_basket_payment_session("cs_test_2", scope=None)
    assert exc.value.status_code == 404


def test_an_unknown_session_is_not_found(monkeypatch):
    def missing(_id):
        raise payment.stripe.error.InvalidRequestError("No such checkout.session", param="id")
    monkeypatch.setattr(payment.stripe.checkout.Session, "retrieve", missing)
    with pytest.raises(HTTPException) as exc:
        payment.get_basket_payment_session("cs_test_missing", scope=None)
    assert exc.value.status_code == 404


def test_anything_but_a_session_id_is_refused_before_stripe(monkeypatch):
    def never(_id):
        raise AssertionError("must not reach Stripe")
    monkeypatch.setattr(payment.stripe.checkout.Session, "retrieve", never)
    with pytest.raises(HTTPException) as exc:
        payment.get_basket_payment_session("pi_123", scope=None)
    assert exc.value.status_code == 400


BASKET_ID = "68b2d1f0a4b21d0f8c9e8801"


def _owned_by(monkeypatch, client):
    _retrieve_returning(monkeypatch, _session(
        id="cs_test_5", status="complete", payment_status="paid",
        # The metadata claims acme; ownership must come from the stored basket regardless.
        metadata={"basket_id": BASKET_ID, "client": "acme"}))
    baskets = MagicMock()
    baskets.find_one.return_value = {"Basket": [{"client": client}]} if client is not None else None
    monkeypatch.setattr(payment, "basket_collection", baskets)


def test_a_pinned_caller_reads_its_own_tenants_session(monkeypatch):
    _owned_by(monkeypatch, "acme")
    assert payment.get_basket_payment_session("cs_test_5", scope="acme")["payment_status"] == "paid"


def test_a_pinned_caller_cannot_read_another_tenants_session(monkeypatch):
    _owned_by(monkeypatch, "globex")
    with pytest.raises(HTTPException) as exc:
        payment.get_basket_payment_session("cs_test_5", scope="acme")
    assert exc.value.status_code == 404


def test_a_pinned_caller_cannot_read_a_session_whose_basket_is_gone(monkeypatch):
    _owned_by(monkeypatch, None)
    with pytest.raises(HTTPException) as exc:
        payment.get_basket_payment_session("cs_test_5", scope="acme")
    assert exc.value.status_code == 404


def test_a_basket_with_no_owner_is_not_a_pinned_callers(monkeypatch):
    _owned_by(monkeypatch, "")
    with pytest.raises(HTTPException) as exc:
        payment.get_basket_payment_session("cs_test_5", scope="acme")
    assert exc.value.status_code == 404


QUOTE_A = "6ae35e26d134320b5e6afc48"
QUOTE_B = "6ae35e26d134320b5e6afc49"


def _quoted(monkeypatch, quote_ids, owners):
    """A basket whose lines carry only quote ids, as `POST /basket/add` stores them, with
    `owners` mapping each quote id to the ClientKey its quote was stored under."""
    _retrieve_returning(monkeypatch, SimpleNamespace(
        id="cs_test_6", status="complete", payment_status="paid",
        metadata={"basket_id": BASKET_ID, "client": ""}))
    baskets = MagicMock()
    baskets.find_one.return_value = {"Basket": [{"quote_id": q} for q in quote_ids]}
    monkeypatch.setattr(payment, "basket_collection", baskets)
    quotes = MagicMock()

    def count(q):
        ids = {str(i) for i in q["_id"]["$in"]}
        return sum(1 for qid, owner in owners.items() if qid in ids and owner == q["clientKey"])

    quotes.count_documents.side_effect = count
    monkeypatch.setattr(payment, "quotes_collection", quotes)


def test_a_pinned_caller_reads_a_session_for_a_basket_quoted_under_its_key(monkeypatch):
    _quoted(monkeypatch, [QUOTE_A], {QUOTE_A: "AO12345"})
    assert payment.get_basket_payment_session("cs_test_6", scope="AO12345")["payment_status"] == "paid"


def test_a_basket_quoted_under_another_key_is_not_found(monkeypatch):
    _quoted(monkeypatch, [QUOTE_A], {QUOTE_A: "OTHER999"})
    with pytest.raises(HTTPException) as exc:
        payment.get_basket_payment_session("cs_test_6", scope="AO12345")
    assert exc.value.status_code == 404


def test_a_basket_only_partly_quoted_under_the_key_is_not_found(monkeypatch):
    _quoted(monkeypatch, [QUOTE_A, QUOTE_B], {QUOTE_A: "AO12345", QUOTE_B: "OTHER999"})
    with pytest.raises(HTTPException) as exc:
        payment.get_basket_payment_session("cs_test_6", scope="AO12345")
    assert exc.value.status_code == 404
