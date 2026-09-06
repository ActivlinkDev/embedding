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
        [_seller(499.0), _seller(529.0), _seller(639.0)], "GBP"
    )
    assert stats == {"currency": "GBP", "min": 499.0, "mean": 555.67, "count": 3}


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
            [_raw_seller(499.0), _raw_seller(529.0, title="Very"), _raw_seller(639.0, title="Currys")],
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
