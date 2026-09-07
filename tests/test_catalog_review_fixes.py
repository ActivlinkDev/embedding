from bson import ObjectId
from fastapi import BackgroundTasks, HTTPException
import pytest

from routers.enrich import dseo_webhook
from routers.sku import create_custom_sku, create_master_sku


def test_icecat_nested_model_image_and_canonical_category(monkeypatch):
    monkeypatch.setattr(
        create_master_sku,
        "_icecat",
        lambda *_: {
            "GeneralInfo": {
                "Brand": "Bosch",
                "ProductNameInfo": {"ProductIntName": {"Value": "SMS6ZCI00G"}},
                "Category": {"Name": {"Value": "Supplier dish cleaning"}},
            },
            "Image": {"HighPic": "https://example.test/high.jpg"},
        },
    )
    monkeypatch.setattr(create_master_sku, "_go_upc", lambda *_: {})
    monkeypatch.setattr(
        create_master_sku,
        "_canonical_category",
        lambda category_input, explicit: "Dishwasher",
    )

    product = create_master_sku._extract_product_data(
        create_master_sku.MasterSKURequest(GTIN="5012345678900", locale="en_GB")
    )

    assert product["model"] == "SMS6ZCI00G"
    assert product["imageUrl"] == "https://example.test/high.jpg"
    assert product["category"] == "Dishwasher"


def test_existing_custom_sku_rejects_a_different_explicit_master(monkeypatch):
    current_master_id = ObjectId()
    requested_master_id = ObjectId()
    existing = {
        "_id": ObjectId(),
        "masterSkuId": current_master_id,
        "enabledLocales": ["en_GB"],
    }

    monkeypatch.setattr(
        create_custom_sku.catalog,
        "client_for_key",
        lambda _key: {"Client_ID": "client-1"},
    )
    monkeypatch.setattr(
        create_custom_sku.locale_collection,
        "find_one",
        lambda *_args, **_kwargs: {"locale": "en_GB"},
    )
    monkeypatch.setattr(
        create_custom_sku.custom_collection,
        "find_one",
        lambda *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        create_custom_sku.master_collection,
        "find_one",
        lambda query, *_args, **_kwargs: {
            "_id": query["_id"],
            "locales": {"en_GB": {}},
        },
    )

    with pytest.raises(HTTPException) as exc:
        create_custom_sku.create_custom_sku_service(
            create_custom_sku.CustomSKURequest(
                ClientKey="client-key",
                Locale="en_GB",
                SKU="SKU-1",
                Source="test",
                masterSkuId=str(requested_master_id),
            ),
            BackgroundTasks(),
        )

    assert exc.value.status_code == 409


def test_dseo_partial_result_does_not_clear_canonical_price(monkeypatch):
    master_id = ObjectId()
    captured = {}
    monkeypatch.setattr(
        dseo_webhook.locale_collection,
        "find_one",
        lambda *_args, **_kwargs: {"locale": "en_GB"},
    )
    monkeypatch.setattr(
        dseo_webhook.mastersku_collection,
        "find_one",
        lambda *_args, **_kwargs: {"identifiers": {"make": "Acme", "model": "ABC-1"}},
    )
    monkeypatch.setattr(
        dseo_webhook.mastersku_collection,
        "update_one",
        lambda query, update: captured.update({"query": query, "update": update}),
    )

    result = dseo_webhook._process_task({
        "data": {"tag": str(master_id), "location_code": 2826},
        "result": [{"items": [{"title": "Acme ABC-1 at Retailer", "price": None, "currency": None}]}],
    })

    set_values = captured["update"]["$set"]
    assert result["status"] == "ok"
    assert "locales.en_GB.market.referencePrice" not in set_values
    assert "locales.en_GB.market.currency" not in set_values
    assert set_values["locales.en_GB.enrichment.status"] == "found"


def test_icecat_part_code_becomes_the_model_not_the_marketing_name(monkeypatch):
    """The Hisense case, with GeneralInfo shaped as the live Icecat API returns it.

    identifiers.model must be the part number, because it becomes the DataforSEO
    keyword and the string matched against merchant listing titles. Storing the
    marketing name there makes every shopping result miss.
    """
    monkeypatch.setattr(
        create_master_sku,
        "_icecat",
        lambda *_: {
            "GeneralInfo": {
                "Brand": "Hisense",
                "BrandPartCode": "58A6Q",
                "ProductName": '58" A6QTUK 4K Ultra HD Smart TV with Freely',
                "ProductNameInfo": {
                    "ProductIntName": "58A6Q",
                    "ProductLocalName": {
                        "Value": '58" A6QTUK 4K Ultra HD Smart TV with Freely',
                        "Language": "EN",
                    },
                },
                "Title": 'Hisense 58" A6QTUK 4K Ultra HD Smart TV with Freely',
            },
        },
    )
    monkeypatch.setattr(create_master_sku, "_go_upc", lambda *_: {})
    monkeypatch.setattr(
        create_master_sku,
        "_canonical_category",
        lambda category_input, explicit: "LED Television",
    )

    product = create_master_sku._extract_product_data(
        create_master_sku.MasterSKURequest(GTIN="6942351416281", locale="en_GB")
    )

    assert product["make"] == "Hisense"
    assert product["model"] == "58A6Q"
    assert product["title"] == 'Hisense 58" A6QTUK 4K Ultra HD Smart TV with Freely'


def test_marketing_name_titles_the_sku_when_icecat_omits_title(monkeypatch):
    monkeypatch.setattr(
        create_master_sku,
        "_icecat",
        lambda *_: {
            "GeneralInfo": {
                "Brand": "Hisense",
                "ProductNameInfo": {"ProductIntName": "58A6Q"},
                "ProductName": '58" A6QTUK 4K Ultra HD Smart TV with Freely',
            },
        },
    )
    monkeypatch.setattr(create_master_sku, "_go_upc", lambda *_: {})
    monkeypatch.setattr(
        create_master_sku,
        "_canonical_category",
        lambda category_input, explicit: "LED Television",
    )

    product = create_master_sku._extract_product_data(
        create_master_sku.MasterSKURequest(GTIN="6942351416281", locale="en_GB")
    )

    assert product["model"] == "58A6Q"
    assert product["title"] == '58" A6QTUK 4K Ultra HD Smart TV with Freely'


def test_explicit_model_still_wins_over_icecat_product_code(monkeypatch):
    monkeypatch.setattr(
        create_master_sku,
        "_icecat",
        lambda *_: {"GeneralInfo": {"Brand": "Hisense", "BrandPartCode": "58A6Q"}},
    )
    monkeypatch.setattr(create_master_sku, "_go_upc", lambda *_: {})
    monkeypatch.setattr(
        create_master_sku,
        "_canonical_category",
        lambda category_input, explicit: "LED Television",
    )

    product = create_master_sku._extract_product_data(
        create_master_sku.MasterSKURequest(
            Make="Hisense", Model="58A6QTUK", GTIN="6942351416281", locale="en_GB"
        )
    )

    assert product["model"] == "58A6QTUK"


def _hisense_icecat(language="EN", title_info=None):
    """The live Icecat response for the Hisense 58A6Q, trimmed to the mapped fields."""
    return {
        "GeneralInfo": {
            "IcecatId": 130727235,
            "Brand": "Hisense",
            "BrandPartCode": "58A6Q",
            "Title": 'Hisense 58" A6QTUK 4K Ultra HD Smart TV with Freely',
            "TitleInfo": _DEFAULT_TITLE_INFO if title_info is None else title_info,
            "ProductName": '58" A6QTUK 4K Ultra HD Smart TV with Freely',
            "ProductNameInfo": {
                "ProductIntName": "58A6Q",
                "ProductLocalName": {
                    "Value": '58" A6QTUK 4K Ultra HD Smart TV with Freely',
                    "Language": "EN",
                },
            },
            "SummaryDescription": {
                "ShortSummaryDescription": 'Hisense 58" A6QTUK, 147.3 cm (58"), Black',
                "LongSummaryDescription": 'Hisense 58" A6QTUK 4K Ultra HD Smart TV with Freely. Display diagonal: 147.3 cm (58").',
            },
            "GeneratedBulletPoints": {
                "Language": language,
                "Values": ['Flat 147.3 cm (58") LED Direct-LED', "4K Ultra HD 3840 x 2160 pixels 16:9"],
            },
            "GTIN": ["6942351416281"],
        },
        "Image": {
            "HighPic": "https://images.icecat.biz/img/gallery/high.jpg",
            "Pic500x500": "https://images.icecat.biz/img/gallery_mediums/medium.jpg",
        },
        "FeaturesGroups": [
            {
                "FeatureGroup": {"Name": {"Value": "Display", "Language": "EN"}},
                "Features": [
                    {
                        "PresentationValue": '147.3 cm (58")',
                        "Value": "58",
                        "Feature": {"Name": {"Value": "Display diagonal", "Language": "EN"}},
                    },
                    {
                        "PresentationValue": "3840 x 2160 pixels",
                        "Feature": {"Name": {"Value": "Display resolution", "Language": "EN"}},
                    },
                ],
            },
            {
                "FeatureGroup": {"Name": {"Value": "Power", "Language": "EN"}},
                "Features": [
                    {
                        "PresentationValue": "130 W",
                        "Feature": {"Name": {"Value": "Power consumption (typical)", "Language": "EN"}},
                    },
                ],
            },
        ],
        "Gallery": [
            {"Pic": "https://images.icecat.biz/img/gallery/high.jpg",
             "Pic500x500": "https://images.icecat.biz/img/gallery_mediums/medium.jpg"},
        ],
        "Multimedia": [],
    }


_DEFAULT_TITLE_INFO = {
    "GeneratedIntTitle": "Hisense 58A6Q TV",
    "GeneratedLocalTitle": {
        "Value": 'Hisense 58A6Q TV 147.3 cm (58") Smart TV Wi-Fi Black',
        "Language": "EN",
    },
    "BrandLocalTitle": {
        "Value": 'Hisense 58" A6QTUK 4K Ultra HD Smart TV with Freely',
        "Language": "EN",
    },
}


def _extract(monkeypatch, icecat, locale="en_GB"):
    monkeypatch.setattr(create_master_sku, "_icecat", lambda *_: icecat)
    monkeypatch.setattr(create_master_sku, "_go_upc", lambda *_: {})
    monkeypatch.setattr(create_master_sku, "_canonical_category", lambda *_: "LED Television")
    return create_master_sku._extract_product_data(
        create_master_sku.MasterSKURequest(GTIN="6942351416281", locale=locale)
    )


def test_icecat_copy_is_mapped_at_creation(monkeypatch):
    """Description, features and specs come from Icecat, not only from a later DSEO round-trip."""
    product = _extract(monkeypatch, _hisense_icecat())

    assert product["description"].startswith('Hisense 58" A6QTUK 4K Ultra HD Smart TV with Freely.')
    assert product["features"] == [
        'Flat 147.3 cm (58") LED Direct-LED',
        "4K Ultra HD 3840 x 2160 pixels 16:9",
    ]
    # PresentationValue carries the unit, and groups are flattened into one map.
    assert product["specifications"]["Display diagonal"] == '147.3 cm (58")'
    assert product["specifications"]["Power consumption (typical)"] == "130 W"
    assert product["icecatId"] == "130727235"


def test_primary_image_prefers_display_size_over_print_resolution(monkeypatch):
    product = _extract(monkeypatch, _hisense_icecat())

    assert product["imageUrl"] == "https://images.icecat.biz/img/gallery_mediums/medium.jpg"
    assert product["highResImage"] == "https://images.icecat.biz/img/gallery/high.jpg"
    assert product["gallery"] == ["https://images.icecat.biz/img/gallery_mediums/medium.jpg"]


def test_locale_specific_title_prefers_the_brands_localised_title(monkeypatch):
    icecat = _hisense_icecat()
    icecat["GeneralInfo"]["TitleInfo"] = {
        "GeneratedIntTitle": "Hisense 58A6Q TV",
        "GeneratedLocalTitle": {"Value": "Hisense 58A6Q TV 147,3 cm Smart TV", "Language": "FR"},
        "BrandLocalTitle": {"Value": 'Hisense 58" A6QTUK TV 4K Ultra HD avec Freely', "Language": "FR"},
    }

    product = _extract(monkeypatch, icecat, locale="fr_FR")

    assert product["title"] == 'Hisense 58" A6QTUK TV 4K Ultra HD avec Freely'
    # The part number is language-independent and must not pick up localised copy.
    assert product["model"] == "58A6Q"


def test_localised_title_in_another_language_loses_to_the_untagged_title(monkeypatch):
    """Icecat serves English copy when it holds none for the requested locale.

    A French journey should fall back to the international title rather than
    presenting an English string that claims to be the localised one.
    """
    icecat = _hisense_icecat(language="EN")
    icecat["GeneralInfo"]["TitleInfo"] = {
        "GeneratedIntTitle": "Hisense 58A6Q TV",
        "BrandLocalTitle": {"Value": "SOME ENGLISH FALLBACK", "Language": "EN"},
    }

    product = _extract(monkeypatch, icecat, locale="fr_FR")

    assert product["title"] == 'Hisense 58" A6QTUK 4K Ultra HD Smart TV with Freely'


def test_locale_block_carries_the_mapped_copy(monkeypatch):
    """The mapped fields must land in the per-locale block, not at document root."""
    icecat = _hisense_icecat()
    monkeypatch.setattr(create_master_sku, "_icecat", lambda *_: icecat)
    monkeypatch.setattr(create_master_sku, "_go_upc", lambda *_: {})
    monkeypatch.setattr(create_master_sku, "_canonical_category", lambda *_: "LED Television")
    monkeypatch.setattr(
        create_master_sku.locale_collection, "find_one",
        lambda *_a, **_k: {"locale": "en_GB", "currency": "GBP"},
    )
    monkeypatch.setattr(create_master_sku.catalog, "find_master", lambda **_k: (None, None))
    monkeypatch.setattr(create_master_sku, "_masked_url", lambda url, base: url)

    captured = {}

    def fake_find_one_and_update(_filter, update, **_kwargs):
        captured.update(update)
        return {"_id": ObjectId(), "identifiers": {}, "locales": {}}

    monkeypatch.setattr(
        create_master_sku.master_collection, "find_one_and_update", fake_find_one_and_update
    )
    monkeypatch.setattr(create_master_sku.master_collection, "update_one", lambda *_a, **_k: None)
    monkeypatch.setattr(
        create_master_sku.master_collection, "find_one",
        lambda *_a, **_k: {"_id": ObjectId(), "identifiers": {}, "locales": {}},
    )

    create_master_sku.create_master_sku_service(
        create_master_sku.MasterSKURequest(GTIN="6942351416281", locale="en_GB"),
        BackgroundTasks(),
        None,
        False,
    )

    block = captured["$set"]["locales.en_GB"]
    assert block["title"] == 'Hisense 58" A6QTUK 4K Ultra HD Smart TV with Freely'
    assert block["description"]
    assert block["features"]
    assert block["specifications"]["Display diagonal"] == '147.3 cm (58")'
    assert block["assets"]["gallery"]
    assert captured["$setOnInsert"]["identifiers"]["model"] == "58A6Q"
    assert captured["$setOnInsert"]["provenance"]["icecatId"] == "130727235"


# --- the Beko DVN04X20W en_GB locale ----------------------------------------
#
# A shopping match on the model number alone landed on an eBay spare-parts
# listing. Its title replaced the Icecat one and its 11.99 GBP became the price
# the quote rated a full-size dishwasher on.


def _dseo_task(master_id, title, price, currency="GBP"):
    return {
        "data": {"tag": str(master_id), "location_code": 2826},
        "result": [{"items": [{
            "title": title,
            "price": price,
            "currency": currency,
            "seller": "eBay - domestic-electricals",
            "product_id": "8428454892147097331",
        }]}],
    }


def _dseo_master(monkeypatch, captured, locale_title, category="Dishwasher"):
    monkeypatch.setattr(
        dseo_webhook.locale_collection, "find_one", lambda *_a, **_k: {"locale": "en_GB"}
    )
    monkeypatch.setattr(
        dseo_webhook.mastersku_collection,
        "find_one",
        lambda *_a, **_k: {
            "identifiers": {"make": "Beko", "model": "DVN04X20W"},
            "category": category,
            "locales": {"en_GB": {"title": locale_title}},
        },
    )
    monkeypatch.setattr(
        dseo_webhook.mastersku_collection,
        "update_one",
        lambda query, update: captured.update({"query": query, "update": update}),
    )


def test_a_shopping_result_never_overwrites_the_catalogue_title(monkeypatch):
    """The SERP title is a merchant's listing copy, not the product's name."""
    captured = {}
    _dseo_master(monkeypatch, captured, "Beko DVN04X20W Freestanding Dishwasher")

    result = dseo_webhook._process_task(
        _dseo_task(ObjectId(), "Beko Din15c20 Dvn04x20w Din15x20 Bdfn15420", 449.0)
    )

    assert result["status"] == "ok"
    assert "locales.en_GB.title" not in captured["update"]["$set"]


def test_a_locale_with_no_title_is_filled_from_the_shopping_result(monkeypatch):
    """Anything beats blank — this is the only case the SERP title is used."""
    captured = {}
    _dseo_master(monkeypatch, captured, "")

    dseo_webhook._process_task(_dseo_task(ObjectId(), "Beko DVN04X20W Dishwasher", 449.0))

    assert captured["update"]["$set"]["locales.en_GB.title"] == "Beko DVN04X20W Dishwasher"


def test_a_price_below_the_category_floor_stores_nothing(monkeypatch):
    """11.99 for a dishwasher means the match is a spare part, not the appliance.

    referencePrice feeds the quote, so this must not reach the locale — and nor
    must the rest of the item, which describes that same wrong listing.
    """
    captured = {}
    _dseo_master(monkeypatch, captured, "Beko DVN04X20W Freestanding Dishwasher")
    monkeypatch.setattr(
        dseo_webhook,
        "min_market_price",
        lambda category, currency: 80.0 if category == "Dishwasher" else None,
    )

    result = dseo_webhook._process_task(
        _dseo_task(ObjectId(), "Beko Din15c20 Dvn04x20w Din15x20 Bdfn15420", 11.99)
    )

    set_values = captured["update"]["$set"]
    assert result["status"] == "rejected"
    assert "locales.en_GB.market.referencePrice" not in set_values
    assert "locales.en_GB.title" not in set_values
    assert set_values["locales.en_GB.enrichment.status"] == "rejected"
    assert "11.99" in set_values["locales.en_GB.enrichment.rejectedReason"]


def test_a_rejected_match_does_not_schedule_the_product_info_round(monkeypatch):
    """product_id belongs to the wrong listing; following it compounds the error."""
    captured = {}
    _dseo_master(monkeypatch, captured, "Beko DVN04X20W Freestanding Dishwasher")
    monkeypatch.setattr(dseo_webhook, "min_market_price", lambda *_a: 80.0)

    result = dseo_webhook._process_task(_dseo_task(ObjectId(), "Beko DVN04X20W part", 11.99))

    # The dispatcher gates both the product_info follow-up and the quote-cache
    # re-warm on status == "ok".
    assert result["status"] != "ok"
    assert "product_id" not in result


def test_a_priced_appliance_still_enriches_normally(monkeypatch):
    """The floor must not stand between a real match and the locale."""
    captured = {}
    _dseo_master(monkeypatch, captured, "Beko DVN04X20W Freestanding Dishwasher")
    monkeypatch.setattr(dseo_webhook, "min_market_price", lambda *_a: 80.0)

    result = dseo_webhook._process_task(
        _dseo_task(ObjectId(), "Beko DVN04X20W Dishwasher", 449.0)
    )

    set_values = captured["update"]["$set"]
    assert result["status"] == "ok"
    assert set_values["locales.en_GB.market.referencePrice"] == 449.0
    assert set_values["locales.en_GB.enrichment.status"] == "found"


def test_product_info_below_the_floor_leaves_the_locale_alone(monkeypatch):
    """The cheapest seller is where a mis-match shows up first.

    This round overwrites description, features, specs and gallery as well as
    the price, so an implausible cheapest offer has to stop all of it — the
    alternative is Icecat copy replaced by an accessory's.
    """
    captured = {}
    monkeypatch.setattr(
        dseo_webhook.locale_collection, "find_one", lambda *_a, **_k: {"locale": "en_GB"}
    )
    monkeypatch.setattr(
        dseo_webhook.mastersku_collection,
        "find_one",
        lambda *_a, **_k: {
            "category": "Dishwasher",
            "locales": {"en_GB": {"market": {"currency": "GBP"}}},
        },
    )
    monkeypatch.setattr(
        dseo_webhook.mastersku_collection,
        "update_one",
        lambda query, update: captured.update({"query": query, "update": update}),
    )
    monkeypatch.setattr(dseo_webhook, "min_market_price", lambda *_a: 80.0)

    result = dseo_webhook._process_product_info_task({
        "data": {"tag": str(ObjectId()), "location_code": 2826, "function": "product_info"},
        "result": [{"items": [{
            "title": "Beko Dishwasher Detergent Tablet Drawer Dispenser",
            "description": "Genuine Beko spare part",
            "sellers": [{"title": "bekoofficialspares", "price": {"current": 46.49, "currency": "GBP"}}],
        }]}],
    })

    set_values = captured["update"]["$set"]
    assert result["status"] == "rejected"
    assert "locales.en_GB.market.referencePrice" not in set_values
    assert "locales.en_GB.market.sellers" not in set_values
    assert "locales.en_GB.description" not in set_values
    assert "46.49" in set_values["locales.en_GB.enrichment.rejectedReason"]
