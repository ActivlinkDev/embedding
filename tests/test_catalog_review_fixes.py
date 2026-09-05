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
