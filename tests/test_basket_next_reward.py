"""The "add one more and save" nudge: which unearned reward is nearest.

`_best_next_reward` looks one step ahead of what the basket already earns, so these
tests pin the boundaries — a tier already reached is not a nudge, and the reward the
basket is closest to wins over a bigger one further away.
"""

import pytest

from routers.basket.ratebasket import _best_next_reward, _next_reward_for_rule


def line(price_pence=1000, **overrides):
    item = {"rounded_price_pence": price_pence, "mode": "payment", "poc": 24}
    item.update(overrides)
    return item


def tiered(name="Multi-device", tiers=None, priority=0, rule_id="r1", constraints=None):
    return {
        "_id": rule_id,
        "name": name,
        "priority": priority,
        "ruleType": "TIERED_PERCENT",
        "constraints": constraints or {},
        "ruleParams": {"tiers": tiers or [{"minItems": 2, "percentOff": 10}]},
    }


def bundle(name="Two for £75", bundles=None, priority=0, rule_id="b1", constraints=None):
    return {
        "_id": rule_id,
        "name": name,
        "priority": priority,
        "ruleType": "FIXED_PRICE_BUNDLE",
        "constraints": constraints or {},
        "ruleParams": {"bundles": bundles or [{"bundleSize": 2, "fixedPricePence": 7500}]},
    }


def test_tier_out_of_reach_is_reported_with_the_shortfall():
    reward = _next_reward_for_rule(tiered(tiers=[{"minItems": 3, "percentOff": 15}]), [line()])
    assert reward is not None
    assert reward.items_needed == 2
    assert reward.percent_off == 15


def test_tier_already_earned_is_not_a_nudge():
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}])
    assert _next_reward_for_rule(rule, [line(), line()]) is None


def test_next_tier_up_is_a_nudge_even_when_a_lower_tier_applies():
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}, {"minItems": 4, "percentOff": 20}])
    reward = _next_reward_for_rule(rule, [line(), line()])
    assert reward is not None
    assert reward.items_needed == 2
    assert reward.percent_off == 20


def test_a_higher_tier_worth_no_more_is_not_a_nudge():
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}, {"minItems": 5, "percentOff": 10}])
    assert _next_reward_for_rule(rule, [line(), line()]) is None


def test_bundle_reports_size_and_price():
    reward = _next_reward_for_rule(bundle(), [line()])
    assert reward is not None
    assert (reward.items_needed, reward.bundle_size, reward.bundle_price_pence) == (1, 2, 7500)


def test_bundle_already_filled_looks_to_the_next_size_up():
    rule = bundle(bundles=[{"bundleSize": 2, "fixedPricePence": 7500}, {"bundleSize": 4, "fixedPricePence": 12000}])
    reward = _next_reward_for_rule(rule, [line(), line()])
    assert reward is not None
    assert (reward.items_needed, reward.bundle_size) == (2, 4)


def test_only_lines_the_rule_applies_to_count_towards_it():
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}])
    rule["appliesTo"] = {"productIds": ["EW1"]}
    # Two lines in the basket, but only one is a qualifying product.
    reward = _next_reward_for_rule(rule, [line(product_id="EW1"), line(product_id="OTHER")])
    assert reward is not None
    assert reward.items_needed == 1


def test_constraint_groups_are_counted_separately():
    # sameTermRequired splits a 24m line from a 36m one, so neither group has two.
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}], constraints={"sameTermRequired": True})
    reward = _next_reward_for_rule(rule, [line(poc=24), line(poc=36)])
    assert reward is not None
    assert reward.items_needed == 1


def test_the_closest_reward_wins_over_a_larger_one_further_away():
    near = tiered(name="near", tiers=[{"minItems": 2, "percentOff": 5}], rule_id="near")
    far = tiered(name="far", tiers=[{"minItems": 5, "percentOff": 50}], rule_id="far")
    reward = _best_next_reward([far, near], [line()])
    assert reward is not None
    assert reward.name == "near"


def test_priority_breaks_a_tie_on_distance():
    low = tiered(name="low", tiers=[{"minItems": 2, "percentOff": 10}], priority=1, rule_id="low")
    high = tiered(name="high", tiers=[{"minItems": 2, "percentOff": 10}], priority=9, rule_id="high")
    reward = _best_next_reward([low, high], [line()])
    assert reward is not None
    assert reward.name == "high"


def test_no_rules_within_reach_yields_no_nudge():
    assert _best_next_reward([tiered(tiers=[{"minItems": 1, "percentOff": 10}])], [line()]) is None


def test_a_broken_rule_does_not_break_the_nudge():
    broken = {"_id": "x", "name": "broken", "ruleType": "TIERED_PERCENT", "ruleParams": {"tiers": "not-a-list"}}
    good = tiered(name="good", tiers=[{"minItems": 2, "percentOff": 10}], rule_id="good")
    reward = _best_next_reward([broken, good], [line()])
    assert reward is not None
    assert reward.name == "good"


def test_unsupported_rule_types_are_ignored():
    assert _next_reward_for_rule({"_id": "z", "name": "z", "ruleType": "MYSTERY"}, [line()]) is None
