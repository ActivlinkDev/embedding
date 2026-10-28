"""device_working: from registration, through the stored device, into assignment.

The matching rules themselves are covered in `test_assignment_matching.py`. These
tests pin the plumbing — that a faulty device registered as faulty is still faulty
by the time the rules see it, which is the part a refactor breaks silently.
"""

from unittest.mock import MagicMock

import pytest
from bson import ObjectId

from routers import assign_product_by_device_id as by_device
from routers import device_register as register
from routers import product_assignment as pa


# ---- registration stores the answer ----

def _register(monkeypatch, **unique):
    clients = MagicMock()
    clients.find_one.return_value = {"Client_ID": "ACME-UK", "ClientKey": "acme_uk_live"}
    monkeypatch.setattr(register, "clients_collection", clients)

    locales = MagicMock()
    locales.find_one.return_value = {"locale": "en_GB", "currency": "GBP"}
    monkeypatch.setattr(register, "locale_params_collection", locales)

    devices = MagicMock()
    devices.find_one.return_value = None
    devices.insert_one.return_value = MagicMock(inserted_id=ObjectId())
    monkeypatch.setattr(register, "devices_collection", devices)

    catalog = MagicMock()
    catalog.resolve_lookup.return_value = (
        {"product": {"category": "Washer Dryer", "price": 449.99, "guarantee": {"labourMonths": 12}},
         "customSkuId": "c1", "masterSkuId": "m1"},
        None,
    )
    monkeypatch.setattr(register, "catalog", catalog)

    payload = register.SimpleRegisterRequest(
        clientkey="acme_uk_live", locale="en_GB", source="web",
        Devices=[register.DeviceModel(
            Identifiers={"make": "Beko", "model": "WTB1000X1W"},
            Unique_Parameters={"serial": "SN-1", "purchase_date": "2025-05-01",
                               "price": 449.99, **unique},
        )],
    )
    register.device_register(payload)
    return devices.insert_one.call_args[0][0]


@pytest.mark.parametrize("sent,stored", [
    ({"device_working": False}, False),
    ({"device_working": True}, True),
    ({}, None),
])
def test_registration_stores_what_the_customer_said(monkeypatch, sent, stored):
    doc = _register(monkeypatch, **sent)
    assert doc["registrationParameters"]["deviceWorking"] is stored


# ---- the stored answer reaches the rules ----

def _assign(monkeypatch, stored, override=None):
    devices = MagicMock()
    devices.find_one.return_value = {
        "_id": ObjectId(),
        "client": "ACME-UK",
        "locale": "en_GB",
        "source": "web",
        "identifiers": {"category": "Washer Dryer", "gteeLabour": "12"},
        "registrationParameters": {
            "price": 449.99, "currency": "GBP", "purchaseDate": "2025-05-01",
            **({} if stored is _UNSET else {"deviceWorking": stored}),
        },
    }
    monkeypatch.setattr(by_device, "devices_collection", devices)

    seen = {}

    def capture(payload):
        seen["device_working"] = payload.device_working
        return {"input": payload.model_dump(), "age_in_months": 6, "products": [
            {"productId": "EX1", "mode": "payment", "terms": [12]}]}

    monkeypatch.setattr(by_device, "assign_products", capture)
    by_device.assign_product_for_device(str(ObjectId()), device_working=override)
    return seen["device_working"]


_UNSET = object()


@pytest.mark.parametrize("stored,expected", [
    (False, False),
    (True, True),
    (None, None),
    (_UNSET, None),
    ("false", False),   # a device written before the field was a boolean
])
def test_the_stored_state_is_what_assignment_matches_on(monkeypatch, stored, expected):
    assert _assign(monkeypatch, stored) is expected


def test_a_caller_can_override_a_device_that_has_since_broken(monkeypatch):
    """Registered working, broken today: the repair journey must not be offered a warranty."""
    assert _assign(monkeypatch, True, override=False) is False


def test_no_override_leaves_the_stored_state_alone(monkeypatch):
    assert _assign(monkeypatch, False, override=None) is False


# ---- unstated is working, everywhere ----

@pytest.mark.parametrize("value", [None, _UNSET])
def test_a_device_that_never_said_is_treated_as_working(monkeypatch, value):
    stated = _assign(monkeypatch, value)
    assert pa.ProductAssignmentRequest(
        client="ACME-UK", source="web", category="Washer Dryer", price=449.99,
        locale="en_GB", purchase_date="2025-05-01", gtee=12, currency="GBP",
        device_working=stated,
    ).working is True


# ---- the widget cache keeps the two states apart ----

def test_the_widget_cache_never_serves_working_options_to_a_faulty_device(monkeypatch):
    """Two states, one SKU: sharing a cache entry would price the wrong products."""
    from routers import widget_quote as widget

    cache = MagicMock()
    cache.find_one.return_value = None
    monkeypatch.setattr(widget, "widget_cache_collection", cache)

    widget._cache_read("sku", "en_GB", 6, 449.99, 12, "GBP", device_working=False)
    assert cache.find_one.call_args[0][0]["deviceWorking"] is False

    widget._cache_read("sku", "en_GB", 6, 449.99, 12, "GBP", device_working=True)
    # An entry written before the condition existed holds the working-device options,
    # so it still answers this query rather than forcing a full rebuild on deploy.
    assert cache.find_one.call_args[0][0]["deviceWorking"] == {"$in": [True, None]}

    widget._cache_write("sku", "en_GB", 6, 449.99, 12, (0, 500), "GBP", [], device_working=False)
    query, update = cache.update_one.call_args[0][:2]
    assert query["deviceWorking"] is False
    assert update["$set"]["deviceWorking"] is False
