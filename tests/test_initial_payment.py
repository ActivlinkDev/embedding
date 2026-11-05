"""Subscriptions that open with an initial payment covering month 1.

The rating prices the opening payment beside the monthly one; checkout charges it once
and defers the monthly line to month 2; the contract records it as the first payment.
A malformed configuration fails the line rather than selling the plan without it.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

import routers.generate_payment_link as gpl
from routers import rate_request as rr
from routers.basket import payment

TAXONOMY = {"Dishwasher": {"category": "Dishwasher", "group": "Kitchen", "sector": "Home Appliances"}}


@pytest.fixture
def rating(monkeypatch):
    from conftest import seed_taxonomy
    seed_taxonomy(monkeypatch, TAXONOMY)
    doc = {
        "_id": ObjectId(), "currency": "GBP", "products": ["WF1"], "status": "active",
        "baseFee": 4.5,
        "localeFactor": [{"locale": "en_GB", "factor": 1}],
        "pocFactor": {"1": 1},
        "categoryFactor": [{"level": "category", "value": "Dishwasher", "factor": 1}],
        "ageFactor": [{"minMonths": 0, "maxMonths": 120, "factor": 1}],
        "priceFactor": [{"min": 0, "max": 5000, "factor": 1}],
        "multiFactor": [{"min": 1, "max": 10, "factor": 1}],
        "initialPayment": {"baseFee": 149.5, "applyFactors": []},
    }
    ratings = MagicMock()
    ratings.find.side_effect = lambda *a, **k: [doc]
    monkeypatch.setattr(rr, "ratings", ratings)
    monkeypatch.setattr(rr, "error_log_collection", MagicMock())
    monkeypatch.setattr(rr, "_ensure_indexes", lambda: None)
    return doc


def line(**over):
    base = dict(product_id="WF1", currency="GBP", locale="en_GB", poc=1,
                category="Dishwasher", age=6, price=449.99, multi_count=1,
                client="AO", source="POS", mode="subscription")
    base.update(over)
    return rr.RateRequest(**base)


def priced(req):
    grouped, _ = rr.price_and_group([req], log_errors=False)
    return grouped[0]["options"][0]


# ---- rating ----

def test_a_subscription_is_priced_with_its_initial_payment(rating):
    option = priced(line())
    assert option["status"] == "ok"
    assert option["rounded_price"] == 4.99          # monthly, from month 2
    assert option["initial_price"] == 149.99        # today, covering month 1
    assert option["initial_price_pence"] == 14999
    assert option["billing"] == {"initial_pence": 14999, "recurring_pence": 499,
                                 "recurring_from_month": 2}
    assert option["factors"]["initial_payment"] == {"base_fee": 149.5, "applied_factors": []}


def test_named_factors_scale_the_initial_payment(rating):
    rating["initialPayment"] = {"baseFee": 100, "applyFactors": ["category", "price"]}
    rating["categoryFactor"][0]["factor"] = 1.2
    rating["priceFactor"][0]["factor"] = 1.25
    rating["localeFactor"][0]["factor"] = 3     # not named, so not applied
    option = priced(line())
    assert option["initial_rate"] == 150.0
    assert option["initial_price"] == 150.49


def test_a_rating_without_the_block_prices_monthly_from_month_one(rating):
    rating.pop("initialPayment")
    option = priced(line())
    assert option["status"] == "ok"
    assert "initial_price" not in option and "billing" not in option


def test_a_one_off_line_ignores_the_block(rating):
    option = priced(line(mode="payment"))
    assert option["status"] == "ok"
    assert "initial_price" not in option


@pytest.mark.parametrize("block", [
    "149.99",
    {"applyFactors": []},
    {"baseFee": 0},
    {"baseFee": -10},
    {"baseFee": "149.99"},
    {"baseFee": True},
    {"baseFee": 100, "applyFactors": ["colour"]},
    {"baseFee": 100, "applyFactors": "category"},
])
def test_a_malformed_block_fails_the_line(rating, block):
    rating["initialPayment"] = block
    option = priced(line())
    assert option["status"] == "error"
    assert "rounded_price" not in option and "initial_price" not in option


# ---- checkout session ----

def _checkout(**over):
    base = dict(product_name="Cover", unit_amount=499, currency="gbp", quantity=1,
                mode=gpl.ModeEnum.subscription, success_url="https://example.com/ok",
                cancel_url="https://example.com/cancel", internal_reference="basket-1")
    base.update(over)
    return gpl.CheckoutSessionRequest(**base)


@pytest.fixture
def created(monkeypatch):
    seen = {}

    def create(**params):
        seen.update(params)
        return SimpleNamespace(url="https://checkout.stripe.com/c/pay/cs_1", id="cs_1",
                               expires_at=1, status="open", customer=None)
    monkeypatch.setattr(gpl.stripe.checkout.Session, "create", create)
    monkeypatch.setattr(gpl, "shorten_with_tinyurl", lambda url: url)
    return seen


def test_checkout_charges_the_initial_payment_once_and_bills_monthly_from_month_two(created, monkeypatch):
    monkeypatch.setattr(gpl, "first_recurring_charge_at", lambda: 1_800_000_000)
    gpl.generate_checkout_session(_checkout(initial_amount=14999))

    one_off, monthly = created["line_items"]
    assert one_off["price_data"]["unit_amount"] == 14999
    assert "recurring" not in one_off["price_data"]
    assert one_off["quantity"] == 1
    assert monthly["price_data"]["unit_amount"] == 499
    assert monthly["price_data"]["recurring"] == {"interval": "month", "interval_count": 1}
    assert created["subscription_data"]["trial_end"] == 1_800_000_000
    assert created["metadata"]["initial_amount"] == "14999"


def test_without_an_initial_payment_checkout_is_unchanged(created):
    gpl.generate_checkout_session(_checkout())
    assert len(created["line_items"]) == 1
    assert "subscription_data" not in created


def test_an_initial_payment_is_refused_on_a_one_off_checkout():
    with pytest.raises(ValidationError):
        _checkout(mode=gpl.ModeEnum.payment, initial_amount=14999)


@pytest.mark.parametrize("amount", [0, -1])
def test_an_initial_payment_must_be_positive(amount):
    with pytest.raises(ValidationError):
        _checkout(initial_amount=amount)


@pytest.mark.parametrize("start,expected", [
    (datetime(2026, 11, 5, 10, 30, tzinfo=timezone.utc), datetime(2026, 12, 5, 10, 30, tzinfo=timezone.utc)),
    (datetime(2026, 12, 15, tzinfo=timezone.utc), datetime(2027, 1, 15, tzinfo=timezone.utc)),
    (datetime(2027, 1, 31, tzinfo=timezone.utc), datetime(2027, 2, 28, tzinfo=timezone.utc)),
    (datetime(2028, 1, 31, tzinfo=timezone.utc), datetime(2028, 2, 29, tzinfo=timezone.utc)),
])
def test_monthly_billing_starts_the_same_day_next_month(start, expected):
    assert gpl.first_recurring_charge_at(start) == int(expected.timestamp())


# ---- basket checkout ----

def _basket_checkout(monkeypatch, lines, final_total=1497):
    baskets = MagicMock()
    baskets.find_one.return_value = {"_id": ObjectId(), "Basket": lines, "final_total": final_total}
    monkeypatch.setattr(payment, "basket_collection", baskets)
    sent = {}
    monkeypatch.setattr(payment, "generate_checkout_session", lambda req: sent.setdefault("req", req))
    payment.create_basket_payment_session(payment.BasketPaymentRequest(basket_id=str(ObjectId())), None)
    return sent["req"]


def _line(**over):
    base = {"mode": "subscription", "currency": "GBP", "client": "AO",
            "rounded_price_pence": 499, "initial_price_pence": 14999}
    base.update(over)
    return base


def test_a_basket_charges_the_sum_of_its_initial_payments(monkeypatch):
    req = _basket_checkout(monkeypatch, [_line(), _line(), _line(initial_price_pence=9999)])
    assert req.initial_amount == 39997
    assert req.unit_amount == 1497               # monthly total, from month 2


def test_a_basket_without_initial_payments_is_unchanged(monkeypatch):
    req = _basket_checkout(monkeypatch, [_line(initial_price_pence=None)], final_total=499)
    assert req.initial_amount is None


def test_a_basket_mixing_plans_with_and_without_an_initial_payment_is_refused(monkeypatch):
    # Every monthly line is deferred to month 2, so the line without an opening payment
    # would get its first month free.
    with pytest.raises(HTTPException) as exc:
        _basket_checkout(monkeypatch, [_line(), _line(initial_price_pence=None)])
    assert exc.value.status_code == 409


@pytest.mark.parametrize("bad", [0, -100, "14999", True])
def test_a_basket_line_with_an_invalid_initial_payment_is_refused(monkeypatch, bad):
    with pytest.raises(HTTPException) as exc:
        _basket_checkout(monkeypatch, [_line(initial_price_pence=bad)])
    assert exc.value.status_code == 400


# ---- contracts ----

from routers.contract import contract_service as cs


def test_the_first_payment_is_the_initial_payment_when_there_is_one():
    assert cs._first_payment({"price": 4.99, "initial_price": 149.99}) == 149.99
    assert cs._first_payment({"price": 4.99, "initial_price": None}) == 4.99
    assert cs._first_payment({"price": 4.99}) == 4.99


def test_the_opening_invoice_records_the_initial_payment(monkeypatch):
    doc = {"_id": ObjectId(), "status": "PENDING_ACTIVATION", "price": 4.99,
           "initial_price": 149.99, "currency": "GBP", "term_months": 1}
    contracts = MagicMock()
    contracts.find_one.side_effect = [None, doc]   # not yet processed; then the re-read
    contracts.find.return_value = [doc]
    monkeypatch.setattr(cs, "contracts_collection", contracts)
    recorded = []
    monkeypatch.setattr(cs, "_record_payment",
                        lambda d, obj, kind, amount, currency, **_: recorded.append((kind, amount)))
    monkeypatch.setattr(cs, "activate", lambda d, term: None)

    cs.handle_invoice_paid({"id": "in_1", "subscription": "sub_1",
                            "billing_reason": "subscription_create", "currency": "gbp"})
    assert recorded == [("INITIAL", 149.99)]


def test_monthly_renewals_record_the_monthly_price(monkeypatch):
    doc = {"_id": ObjectId(), "status": "ACTIVE", "price": 4.99,
           "initial_price": 149.99, "currency": "GBP", "term_months": 1}
    contracts = MagicMock()
    contracts.find_one.return_value = None
    contracts.find.return_value = [doc]
    monkeypatch.setattr(cs, "contracts_collection", contracts)
    recorded = []
    monkeypatch.setattr(cs, "_record_payment",
                        lambda d, obj, kind, amount, currency, **_: recorded.append((kind, amount)))
    monkeypatch.setattr(cs, "renew", lambda d: d)

    cs.handle_invoice_paid({"id": "in_2", "subscription": "sub_1",
                            "billing_reason": "subscription_cycle", "currency": "gbp"})
    assert recorded == [("RENEWAL", 4.99)]
