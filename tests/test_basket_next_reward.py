"""The "add one more and save" nudge: what the basket is one addition away from.

`_best_next_reward` prices a hypothetical basket with the same evaluator that
prices the real one, so these tests pin the promise rather than the arithmetic:
a reward is reported only when adding lines would genuinely raise the discount
the customer would actually be given, and only when those lines could be added
at all.
"""

import pytest

from routers.basket.ratebasket import _best_next_reward


def line(price_pence=10000, **overrides):
    item = {"rounded_price_pence": price_pence, "mode": "payment", "poc": 24}
    item.update(overrides)
    return item


def tiered(name="Multi-device", tiers=None, priority=0, rule_id="r1", constraints=None, applies=None):
    return {
        "_id": rule_id,
        "name": name,
        "priority": priority,
        "ruleType": "TIERED_PERCENT",
        "constraints": constraints or {},
        "appliesTo": applies or {},
        "ruleParams": {"tiers": tiers or [{"minItems": 2, "percentOff": 10}]},
    }


def bundle(name="Bundle", bundles=None, priority=0, rule_id="b1", constraints=None, repeatable=True):
    return {
        "_id": rule_id,
        "name": name,
        "priority": priority,
        "ruleType": "FIXED_PRICE_BUNDLE",
        "constraints": constraints or {},
        "appliesTo": {},
        "ruleParams": {
            "bundles": bundles or [{"bundleSize": 2, "fixedPricePence": 15000}],
            "repeatable": repeatable,
        },
    }


# ---- The basic promise ----

def test_a_tier_one_line_away_is_reported():
    reward = _best_next_reward([tiered(tiers=[{"minItems": 2, "percentOff": 10}])], [line()])
    assert reward is not None
    assert reward.items_needed == 1
    assert reward.percent_off == 10


def test_a_tier_already_earned_is_not_a_nudge():
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}])
    assert _best_next_reward([rule], [line(), line()]) is None


def test_the_next_tier_up_is_a_nudge_even_while_a_lower_tier_applies():
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}, {"minItems": 4, "percentOff": 20}])
    reward = _best_next_reward([rule], [line(), line()])
    assert reward is not None
    assert (reward.items_needed, reward.percent_off) == (2, 20)


def test_a_higher_tier_worth_no_more_is_not_a_nudge():
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}, {"minItems": 5, "percentOff": 10}])
    assert _best_next_reward([rule], [line(), line()]) is None


def test_an_empty_basket_has_nothing_to_extrapolate_from():
    assert _best_next_reward([tiered()], []) is None


def test_a_reward_further_than_the_look_ahead_is_not_reported():
    rule = tiered(tiers=[{"minItems": 12, "percentOff": 50}])
    assert _best_next_reward([rule], [line()]) is None


def test_the_closest_reward_wins_over_a_larger_one_further_away():
    near = tiered(name="near", tiers=[{"minItems": 2, "percentOff": 5}], rule_id="near")
    far = tiered(name="far", tiers=[{"minItems": 4, "percentOff": 50}], rule_id="far")
    reward = _best_next_reward([far, near], [line()])
    assert reward is not None
    assert reward.name == "near"


def test_priority_breaks_a_tie_on_distance_and_discount():
    low = tiered(name="low", tiers=[{"minItems": 2, "percentOff": 10}], priority=1, rule_id="low")
    high = tiered(name="high", tiers=[{"minItems": 2, "percentOff": 10}], priority=9, rule_id="high")
    reward = _best_next_reward([low, high], [line()])
    assert reward is not None
    assert reward.name == "high"


def test_constraint_groups_are_counted_separately():
    # sameTermRequired splits a 24m line from a 36m one, so neither group has two.
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}], constraints={"sameTermRequired": True})
    reward = _best_next_reward([rule], [line(poc=24), line(poc=36)])
    assert reward is not None
    assert reward.items_needed == 1


def test_only_lines_a_rule_applies_to_count_towards_it():
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 10}], applies={"productIds": ["EW1"]})
    reward = _best_next_reward([rule], [line(product_id="EW1"), line(product_id="OTHER")])
    assert reward is not None
    assert reward.items_needed == 1


def test_no_rules_means_no_nudge():
    assert _best_next_reward([], [line()]) is None


def test_a_broken_rule_does_not_break_the_nudge():
    broken = {"_id": "x", "name": "broken", "ruleType": "TIERED_PERCENT", "ruleParams": {"tiers": "not-a-list"}}
    good = tiered(name="good", tiers=[{"minItems": 2, "percentOff": 10}], rule_id="good")
    reward = _best_next_reward([broken, good], [line()])
    assert reward is not None
    assert reward.name == "good"


def test_unsupported_rule_types_are_ignored():
    assert _best_next_reward([{"_id": "z", "name": "z", "ruleType": "MYSTERY"}], [line()]) is None


# ---- Bundles ----

def test_a_bundle_one_line_away_reports_its_size_and_price():
    reward = _best_next_reward([bundle()], [line()])
    assert reward is not None
    assert (reward.items_needed, reward.bundle_size, reward.bundle_price_pence) == (1, 2, 15000)


def test_a_bundle_whose_fixed_price_saves_nothing_is_not_a_nudge():
    # Two lines at 10000 for a "bundle price" of 25000 is not a discount.
    rule = bundle(bundles=[{"bundleSize": 2, "fixedPricePence": 25000}])
    assert _best_next_reward([rule], [line()]) is None


def test_a_filled_repeatable_bundle_offers_nothing_better():
    # Two more lines would earn a second bundle, but on identical terms — the
    # customer gets the same deal for more money, which is not a reward.
    rule = bundle(bundles=[{"bundleSize": 2, "fixedPricePence": 15000}], repeatable=True)
    assert _best_next_reward([rule], [line(), line()]) is None


def test_a_repeatable_bundle_with_an_odd_line_out_is_one_away():
    # Three lines pair only two of them; the third is paying full price, so one
    # more genuinely improves the deal.
    rule = bundle(bundles=[{"bundleSize": 2, "fixedPricePence": 15000}], repeatable=True)
    reward = _best_next_reward([rule], [line(), line(), line()])
    assert reward is not None
    assert reward.items_needed == 1


def test_a_non_repeatable_bundle_stops_once_it_is_filled():
    rule = bundle(bundles=[{"bundleSize": 2, "fixedPricePence": 15000}], repeatable=False)
    assert _best_next_reward([rule], [line(), line()]) is None


def test_a_capped_bundle_stops_nudging_at_its_cap():
    rule = bundle(bundles=[{"bundleSize": 2, "fixedPricePence": 15000, "capBundles": 1}], repeatable=True)
    assert _best_next_reward([rule], [line(), line()]) is None


def test_a_bundle_below_its_minimum_item_count_reports_the_real_shortfall():
    # A size-2 bundle gated behind minItems 3 is two away from one line, not one.
    rule = bundle(bundles=[{"bundleSize": 2, "fixedPricePence": 15000}], constraints={"minItems": 3})
    reward = _best_next_reward([rule], [line()])
    assert reward is not None
    assert reward.items_needed == 2


def test_a_bundle_at_its_bundle_size_but_below_its_minimum_is_still_one_away():
    rule = bundle(bundles=[{"bundleSize": 2, "fixedPricePence": 15000}], constraints={"minItems": 3})
    reward = _best_next_reward([rule], [line(), line()])
    assert reward is not None
    assert reward.items_needed == 1


# ---- Rewards that could never be given ----

def test_a_weaker_rule_is_not_advertised_over_the_one_already_applied():
    # 20% is already applied and stays the winner; a 5% tier one line away would
    # never be given, so promising it would be a lie.
    applied = tiered(name="strong", tiers=[{"minItems": 1, "percentOff": 20}], rule_id="strong")
    weaker = tiered(name="weak", tiers=[{"minItems": 2, "percentOff": 5}], rule_id="weak")
    assert _best_next_reward([applied, weaker], [line()]) is None


def test_a_stronger_rule_further_away_is_still_advertised():
    applied = tiered(name="strong", tiers=[{"minItems": 1, "percentOff": 20}], rule_id="strong")
    better = tiered(name="better", tiers=[{"minItems": 3, "percentOff": 40}], rule_id="better")
    reward = _best_next_reward([applied, better], [line()])
    assert reward is not None
    assert reward.name == "better"


def test_a_rule_needing_the_billing_mode_this_basket_cannot_take_is_not_advertised():
    # Adding a subscription line to a one-off basket is refused with 409, so a
    # rule that only applies to subscriptions is unreachable from here.
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 30}], applies={"mode": "subscription"})
    assert _best_next_reward([rule], [line(mode="payment")]) is None


def test_a_rule_matching_this_basket_s_own_mode_is_advertised():
    rule = tiered(tiers=[{"minItems": 2, "percentOff": 30}], applies={"mode": "payment"})
    reward = _best_next_reward([rule], [line(mode="payment")])
    assert reward is not None
    assert reward.items_needed == 1


def test_the_cheapest_line_is_the_one_extrapolated_from():
    # A size-2 bundle at 15000 beats 10000 + 20000 but not 10000 + 10000, so
    # modelling the addition on the cheaper line correctly finds no saving.
    rule = bundle(bundles=[{"bundleSize": 2, "fixedPricePence": 21000}])
    assert _best_next_reward([rule], [line(price_pence=10000), line(price_pence=20000)]) is None


def test_more_lines_at_the_same_percentage_is_not_a_reward():
    # 10% of a bigger basket is more money off but not a better offer; nudging on
    # the amount alone would let a basket be nudged forever.
    rule = tiered(tiers=[{"minItems": 1, "percentOff": 10}])
    assert _best_next_reward([rule], [line(), line()]) is None
