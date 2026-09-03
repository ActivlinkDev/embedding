"""One billing mode per basket.

Stripe Checkout takes a single mode per session, so a basket holding both one-off
and recurring cover has no correct outcome. `assert_no_mode_conflict` is what stops
one being built; these tests pin what counts as a conflict.
"""

import pytest
from fastapi import HTTPException

from routers.basket import assert_no_mode_conflict


def line(mode):
    return {"deviceId": "d1", "mode": mode}


def test_same_mode_is_allowed():
    assert_no_mode_conflict([line("payment"), line("payment")], "payment")


def test_empty_basket_accepts_anything():
    assert_no_mode_conflict([], "subscription")


def test_subscription_into_a_one_off_basket_is_refused():
    with pytest.raises(HTTPException) as exc:
        assert_no_mode_conflict([line("payment")], "subscription")
    assert exc.value.status_code == 409
    assert "'payment'" in exc.value.detail
    assert "subscription" in exc.value.detail


def test_one_off_into_a_subscription_basket_is_refused():
    with pytest.raises(HTTPException) as exc:
        assert_no_mode_conflict([line("subscription")], "payment")
    assert exc.value.status_code == 409


def test_conflict_is_caught_behind_matching_lines():
    # The basket already holds the incoming mode, but not only that mode.
    with pytest.raises(HTTPException) as exc:
        assert_no_mode_conflict([line("payment"), line("subscription")], "payment")
    assert exc.value.status_code == 409
    assert "'subscription'" in exc.value.detail


def test_a_line_with_no_mode_cannot_conflict():
    # Lines predating the field say nothing about how they bill.
    assert_no_mode_conflict([{"deviceId": "d1"}, {"deviceId": "d2", "mode": None}], "subscription")


def test_an_incoming_line_with_no_mode_is_not_blocked():
    # Nothing to compare, so nothing to refuse — the same lines still price correctly.
    assert_no_mode_conflict([line("payment")], None)
    assert_no_mode_conflict([line("payment")], "")


def test_the_message_names_both_kinds_of_cover():
    with pytest.raises(HTTPException) as exc:
        assert_no_mode_conflict([line("subscription")], "payment")
    detail = exc.value.detail
    assert "payment" in detail and "subscription" in detail
    # And says what to do about it, not just that it failed.
    assert "start a new one" in detail
