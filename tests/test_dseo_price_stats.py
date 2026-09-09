"""Reference price derivation from the DataforSEO seller list.

The shopping task records the price printed on the Google Shopping tile, which
is a single headline offer and regularly sits well above the real market price
(a Hisense HV673A60UK came back at 639 GBP while every seller listed 499). The
product_info round is the correction: cheapest seller wins.
"""

import pytest
from bson import ObjectId

from routers.enrich import dseo_webhook


def _seller(price, currency="GBP", title="Shop"):
    return {"title": title, "price": price, "currency": currency}


def test_min_and_mean_come_from_the_seller_offers():
    stats = dseo_webhook._price_stats(
        [
            _seller(639.0, title="Currys"),
            _seller(499.0, title="Argos"),
            _seller(529.0, title="Very"),
        ],
        "GBP",
    )
    assert stats == {
        "currency": "GBP",
        "min": 499.0,
        "mean": 555.67,
        "count": 3,
        "merchant": "Argos",
    }


def test_the_named_merchant_is_the_first_at_the_lowest_price():
    """Ties keep Google's ordering — not whichever name sorts first."""
    stats = dseo_webhook._price_stats(
        [_seller(499.0, title="Very"), _seller(499.0, title="Argos")], "GBP"
    )
    assert stats["merchant"] == "Very"


def test_a_cheapest_offer_in_another_currency_cannot_claim_the_merchant():
    stats = dseo_webhook._price_stats(
        [_seller(420.0, "EUR", title="Amazon.de"), _seller(499.0, "GBP", title="Argos")],
        "GBP",
    )
    assert stats["merchant"] == "Argos"
    assert stats["min"] == 499.0


def test_an_anonymous_cheapest_offer_reports_no_merchant():
    stats = dseo_webhook._price_stats([_seller(499.0, title="")], "GBP")
    assert stats["min"] == 499.0
    assert stats["merchant"] is None


def test_offers_in_another_currency_are_excluded():
    """A EUR offer in a GBP locale must not undercut the GBP reference price."""
    stats = dseo_webhook._price_stats(
        [_seller(499.0, "GBP"), _seller(420.0, "EUR"), _seller(560.0, "GBP")], "GBP"
    )
    assert stats["currency"] == "GBP"
    assert stats["min"] == 499.0
    assert stats["count"] == 2


def test_locale_without_a_currency_falls_back_to_the_common_seller_currency():
    stats = dseo_webhook._price_stats(
        [_seller(584.99, "EUR"), _seller(609.97, "EUR"), _seller(499.0, "GBP")], None
    )
    assert stats["currency"] == "EUR"
    assert stats["min"] == 584.99


@pytest.mark.parametrize(
    "sellers",
    [
        [],
        [_seller(None)],
        [_seller("499.00")],
        [_seller(0)],
        [_seller(-10.0)],
    ],
)
def test_unusable_prices_yield_no_stats(sellers):
    """No usable offer must leave the existing reference price untouched, not zero it."""
    assert dseo_webhook._price_stats(sellers, "GBP") is None


class _FakeCollection:
    def __init__(self, doc):
        self._doc = doc
        self.updates = []

    def find_one(self, query, projection=None):
        return self._doc

    def update_one(self, query, update):
        self.updates.append((query, update))


def _product_info_task(master_id, sellers):
    return {
        "data": {"tag": str(master_id), "location_code": 2826, "language_code": "en"},
        "result": [{"items": [{"title": "Hisense HV673A60UK", "sellers": sellers}]}],
    }


def _raw_seller(price, currency="GBP", title="Argos"):
    return {
        "title": title,
        "url": f"https://example.test/{title}",
        "price": {"current": price, "currency": currency},
        "product_availability": "in_stock",
    }


def test_product_info_postback_replaces_the_serp_reference_price(monkeypatch):
    master_id = ObjectId()
    collection = _FakeCollection(
        {"_id": master_id, "locales": {"en_GB": {"market": {"currency": "GBP"}}}}
    )
    monkeypatch.setattr(dseo_webhook, "mastersku_collection", collection)
    monkeypatch.setattr(
        dseo_webhook.locale_collection, "find_one", lambda *a, **k: {"locale": "en_GB"}
    )

    outcome = dseo_webhook._process_product_info_task(
        _product_info_task(
            master_id,
            [
                _raw_seller(639.0, title="Currys"),
                _raw_seller(499.0, title="Argos"),
                _raw_seller(529.0, title="Very"),
            ],
        )
    )

    assert outcome["status"] == "ok"
    assert outcome["price_min"] == 499.0
    _, update = collection.updates[0]
    fields = update["$set"]
    assert fields["locales.en_GB.market.referencePrice"] == 499.0
    assert fields["locales.en_GB.market.priceMin"] == 499.0
    assert fields["locales.en_GB.market.priceMean"] == 555.67
    assert fields["locales.en_GB.market.priceSampleSize"] == 3
    assert fields["locales.en_GB.market.currency"] == "GBP"
    assert fields["locales.en_GB.market.merchant"] == "Argos"


def test_product_info_without_prices_leaves_the_reference_price_alone(monkeypatch):
    master_id = ObjectId()
    collection = _FakeCollection(
        {"_id": master_id, "locales": {"en_GB": {"market": {"currency": "GBP"}}}}
    )
    monkeypatch.setattr(dseo_webhook, "mastersku_collection", collection)
    monkeypatch.setattr(
        dseo_webhook.locale_collection, "find_one", lambda *a, **k: {"locale": "en_GB"}
    )

    outcome = dseo_webhook._process_product_info_task(_product_info_task(master_id, []))

    assert outcome["price_min"] is None
    fields = collection.updates[0][1]["$set"]
    assert "locales.en_GB.market.referencePrice" not in fields
    assert "locales.en_GB.market.priceMin" not in fields
    assert "locales.en_GB.market.merchant" not in fields


def test_an_unnamed_cheapest_seller_keeps_the_shopping_tasks_merchant(monkeypatch):
    """A blank name must not overwrite the merchant the shopping task recorded."""
    master_id = ObjectId()
    collection = _FakeCollection(
        {"_id": master_id, "locales": {"en_GB": {"market": {"currency": "GBP"}}}}
    )
    monkeypatch.setattr(dseo_webhook, "mastersku_collection", collection)
    monkeypatch.setattr(
        dseo_webhook.locale_collection, "find_one", lambda *a, **k: {"locale": "en_GB"}
    )

    dseo_webhook._process_product_info_task(
        _product_info_task(master_id, [_raw_seller(499.0, title="")])
    )

    fields = collection.updates[0][1]["$set"]
    assert fields["locales.en_GB.market.referencePrice"] == 499.0
    assert "locales.en_GB.market.merchant" not in fields


# --- match tightening, title policy and the category price floor -------------
#
# The en_GB locale of a Beko DVN04X20W dishwasher came back titled "Beko
# Din15c20 Dvn04x20w Din15x20 Dvn04x20s Bdfn15420 Dvs04x20x" at 11.99 GBP: the
# model number alone matched an eBay spare-parts listing, whose title then
# overwrote the Icecat one and whose price became what the quote rated on.


def _item(title, price=499.0, currency="GBP", **extra):
    return {"title": title, "price": price, "currency": currency, **extra}


def test_the_make_must_appear_as_well_as_the_model():
    """A bare model number is not an identifier — it matches inside anything."""
    items = [_item("Hotpoint DVN04X20W Dishwasher"), _item("Beko DVN04X20W Dishwasher")]
    assert dseo_webhook._find_matching_item(items, "Beko", "DVN04X20W")["title"] == (
        "Beko DVN04X20W Dishwasher"
    )


def test_no_match_when_only_the_make_is_present():
    items = [_item("Beko DIN15C20 Dishwasher")]
    assert dseo_webhook._find_matching_item(items, "Beko", "DVN04X20W") is None


def test_an_incomplete_sku_matches_nothing():
    """Neither half alone may enrich a SKU with an arbitrary first result."""
    items = [_item("Beko DVN04X20W Dishwasher")]
    assert dseo_webhook._find_matching_item(items, "", "DVN04X20W") is None
    assert dseo_webhook._find_matching_item(items, "Beko", "") is None


def test_matching_still_ignores_case_and_punctuation():
    items = [_item("beko dvn-04-x20w dishwasher")]
    assert dseo_webhook._find_matching_item(items, "BEKO", "DVN04X20W") is not None


class _Tree:
    """Stand in for the taxonomy lookup dseo_webhook imports."""

    def __init__(self, floors):
        self.floors = floors

    def __call__(self, category, currency):
        return (self.floors.get(category) or {}).get((currency or "").upper())


@pytest.fixture
def dishwasher_floor(monkeypatch):
    monkeypatch.setattr(
        dseo_webhook, "min_market_price", _Tree({"Dishwasher": {"GBP": 80.0}})
    )


def test_a_spare_part_price_is_rejected_for_the_category(dishwasher_floor):
    reason = dseo_webhook._implausible_price(11.99, "GBP", "Dishwasher")
    assert reason and "11.99" in reason and "Dishwasher" in reason


def test_a_real_appliance_price_passes(dishwasher_floor):
    assert dseo_webhook._implausible_price(449.99, "GBP", "Dishwasher") is None


def test_a_price_exactly_on_the_floor_passes(dishwasher_floor):
    assert dseo_webhook._implausible_price(80.0, "GBP", "Dishwasher") is None


def test_a_category_with_no_floor_accepts_anything(dishwasher_floor):
    """Most categories carry no floor; those must behave as they did before."""
    assert dseo_webhook._implausible_price(11.99, "GBP", "Toaster") is None
    assert dseo_webhook._implausible_price(11.99, "GBP", "") is None


def test_a_currency_the_floor_does_not_cover_is_not_judged(dishwasher_floor):
    """A GBP floor says nothing about a TRL price."""
    assert dseo_webhook._implausible_price(11.99, "TRL", "Dishwasher") is None


@pytest.mark.parametrize("price", [None, "11.99", True, {}])
def test_an_unusable_price_is_not_reported_as_implausible(dishwasher_floor, price):
    """Callers already drop these; blaming the category would mislead."""
    assert dseo_webhook._implausible_price(price, "GBP", "Dishwasher") is None


def test_a_legal_entity_make_still_matches_the_brand_in_a_listing():
    """Icecat stores "LG Electronics"; the listing says "LG"."""
    items = [_item("LG DSHD24U Dishwasher")]
    assert dseo_webhook._find_matching_item(items, "LG Electronics", "DSHD24U") is not None


def test_the_full_legal_entity_still_matches_when_a_title_carries_it():
    items = [_item("LG Electronics DSHD24U Dishwasher")]
    assert dseo_webhook._find_matching_item(items, "LG Electronics", "DSHD24U") is not None


def test_a_generic_second_word_cannot_stand_in_for_the_brand():
    """Otherwise "Electronics" alone would pass nearly every title."""
    items = [_item("Hotpoint DSHD24U Electronics Dishwasher")]
    assert dseo_webhook._find_matching_item(items, "LG Electronics", "DSHD24U") is None


def test_a_single_letter_first_word_is_not_accepted_alone():
    """One character means nothing once a title is stripped to alphanumerics."""
    items = [_item("Hotpoint SMS6ZCI00G Dishwasher")]
    assert dseo_webhook._find_matching_item(items, "B Bosch", "SMS6ZCI00G") is None


# --- brand matching on word boundaries --------------------------------------


def test_a_short_brand_does_not_match_inside_another_word():
    """"GE" folds into "fridge", "range" and "storage" — half of appliance copy."""
    items = [_item("Samsung ABC123 fridge")]
    assert dseo_webhook._find_matching_item(items, "GE", "ABC123") is None


def test_a_short_brand_still_matches_as_its_own_word():
    items = [_item("GE ABC123 Refrigerator")]
    assert dseo_webhook._find_matching_item(items, "GE", "ABC123") is not None


def test_a_brand_does_not_match_as_the_tail_of_a_longer_word():
    items = [_item("Hotpoint Beko-compatible DVN04X20W hose")]
    assert dseo_webhook._find_matching_item(items, "Eko", "DVN04X20W") is None


def test_a_punctuated_model_still_matches_loosely():
    """Sellers punctuate model numbers freely; only the brand must be exact."""
    items = [_item("Beko DVN-04-X20W Dishwasher")]
    assert dseo_webhook._find_matching_item(items, "Beko", "DVN04X20W") is not None


def test_a_spaced_legal_entity_matches_across_consecutive_words():
    items = [_item("LG Electronics DSHD24U Dishwasher")]
    assert dseo_webhook._find_matching_item(items, "LGElectronics", "DSHD24U") is not None


# --- every match is returned, in order ---------------------------------------


def test_all_matching_items_are_returned_in_page_order():
    items = [
        _item("Beko DVN04X20W drawer", price=11.99),
        _item("Beko DVN04X20W Dishwasher", price=449.0),
    ]
    titles = [i["title"] for i in dseo_webhook._matching_items(items, "Beko", "DVN04X20W")]
    assert titles == ["Beko DVN04X20W drawer", "Beko DVN04X20W Dishwasher"]


def test_non_matching_items_are_left_out():
    items = [_item("Hotpoint HDW1 Dishwasher"), _item("Beko DVN04X20W Dishwasher")]
    assert len(dseo_webhook._matching_items(items, "Beko", "DVN04X20W")) == 1
