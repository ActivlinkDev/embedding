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
        lambda *_args, **_kwargs: {"identifiers": {"model": "ABC-1"}},
    )
    monkeypatch.setattr(
        dseo_webhook.mastersku_collection,
        "update_one",
        lambda query, update: captured.update({"query": query, "update": update}),
    )

    result = dseo_webhook._process_task({
        "data": {"tag": str(master_id), "location_code": 2826},
        "result": [{"items": [{"title": "Retailer ABC-1", "price": None, "currency": None}]}],
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
