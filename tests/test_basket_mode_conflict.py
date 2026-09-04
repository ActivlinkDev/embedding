"""One billing mode per basket.

Stripe Checkout takes a single mode per session, so a basket holding both one-off
and recurring cover has no correct outcome. `assert_no_mode_conflict` is what stops
one being built; these tests pin what counts as a conflict.
"""

import pytest
from fastapi import HTTPException

from routers.basket import assert_no_mode_conflict, conflicting_modes, mode_guard_filter


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


# ---- The guard that rides on the write ----
#
# Reading the basket and then appending is two operations, so two opposite-mode
# adds can both pass the check and both push. The append carries this filter so
# Mongo settles it instead.


def matches(filter_clause, basket_items):
    """Evaluate the $not/$elemMatch clause the way Mongo would."""
    clause = filter_clause.get("Basket")
    if clause is None:
        return True
    inner = clause["$not"]["$elemMatch"]["mode"]
    forbidden = inner["$nin"]
    return not any(
        "mode" in it and it["mode"] not in forbidden for it in basket_items
    )


def test_the_filter_admits_a_basket_of_the_same_mode():
    assert matches(mode_guard_filter("payment"), [line("payment"), line("payment")])


def test_the_filter_admits_an_empty_basket():
    assert matches(mode_guard_filter("subscription"), [])


def test_the_filter_rejects_a_basket_of_the_opposite_mode():
    assert not matches(mode_guard_filter("subscription"), [line("payment")])


def test_the_filter_rejects_a_basket_that_is_already_mixed():
    assert not matches(mode_guard_filter("payment"), [line("payment"), line("subscription")])


def test_the_filter_ignores_lines_carrying_no_mode():
    assert matches(mode_guard_filter("subscription"), [{"deviceId": "d1"}, {"deviceId": "d2", "mode": None}])


def test_a_line_with_no_mode_of_its_own_is_unguarded():
    # Nothing to compare against, so the append is not conditioned at all.
    assert mode_guard_filter(None) == {}
    assert mode_guard_filter("") == {}


def test_the_filter_and_the_check_agree():
    # They are used together — the filter decides, the check explains — so they
    # must never disagree about what counts as a conflict.
    baskets = [
        [],
        [line("payment")],
        [line("subscription")],
        [line("payment"), line("subscription")],
        [{"deviceId": "d"}],
        [{"deviceId": "d", "mode": None}, line("payment")],
    ]
    for basket in baskets:
        for mode in ("payment", "subscription"):
            allowed_by_filter = matches(mode_guard_filter(mode), basket)
            allowed_by_check = not conflicting_modes(basket, mode)
            assert allowed_by_filter == allowed_by_check, (basket, mode)
