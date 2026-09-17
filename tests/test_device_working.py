"""`device_working`: registering a broken device, and assigning products to one.

The repair journey (`/assistance`) registers a device the customer has just reported a
fault on, and a client can then point those devices at repair products with a rule whose
`when.deviceWorking` is `false`. Every other caller omits the field, so the tests below
also pin the backwards-compatible reading of an absent value: working, not unknown.
"""

from unittest.mock import MagicMock

import pytest
from bson import ObjectId

from routers import device_register as register_route
from routers import assign_product_by_device_id as assign_route
from routers import product_assignment as pa

TAXONOMY = {
    "Washer Dryer": {"category": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances"},
}


@pytest.fixture(autouse=True)
def taxonomy(monkeypatch):
    from conftest import seed_taxonomy
    seed_taxonomy(monkeypatch, TAXONOMY)


# ---- registration stores it ----

def mock_register_collections(monkeypatch):
    clients = MagicMock()
    clients.find_one.return_value = {"Client_ID": "ACME-UK", "ClientKey": "acme_uk_live"}
    monkeypatch.setattr(register_route, "clients_collection", clients)

    locales = MagicMock()
    locales.find_one.return_value = {"locale": "en_GB", "currency": "GBP"}
    monkeypatch.setattr(register_route, "locale_params_collection", locales)

    devices = MagicMock()
    devices.find_one.return_value = None  # no duplicates
    devices.insert_one.return_value = MagicMock(inserted_id=ObjectId())
    monkeypatch.setattr(register_route, "devices_collection", devices)

    catalog = MagicMock()
    catalog.resolve_lookup.return_value = (None, None)
    monkeypatch.setattr(register_route, "catalog", catalog)

    return devices


def register(unique_parameters):
    device = register_route.DeviceModel(
        Identifiers={"make": "Beko", "model": "WTB1000X1W", "category": "Washer Dryer"},
        Unique_Parameters=unique_parameters,
        allow_manual=True,
    )
    return register_route.SimpleRegisterRequest(
        clientkey="acme_uk_live", locale="en_GB", source="web", Devices=[device]
    )


@pytest.mark.parametrize("sent,stored", [
    (False, False),
    (True, True),
])
def test_device_working_is_stored_on_the_device(monkeypatch, sent, stored):
    devices = mock_register_collections(monkeypatch)
    payload = register({
        "serial": "SN-1", "purchase_date": "2025-05-01", "price": 449.99, "device_working": sent,
    })

    register_route.device_register(payload)

    assert devices.insert_one.call_args.args[0]["registrationParameters"]["deviceWorking"] is stored


def test_a_caller_that_omits_it_registers_a_working_device(monkeypatch):
    """Every existing caller — POS, the cover journey, CSV import — sends no such field."""
    devices = mock_register_collections(monkeypatch)
    payload = register({"serial": "SN-1", "purchase_date": "2025-05-01", "price": 449.99})

    register_route.device_register(payload)

    assert devices.insert_one.call_args.args[0]["registrationParameters"]["deviceWorking"] is True


# ---- assignment reads it off the stored device ----

def stored_device(**registration_parameters):
    params = {
        "purchaseDate": "2025-01-01", "price": 449.99, "currency": "GBP",
        "clientRef": "", "registrationStatus": "unassigned",
    }
    params.update(registration_parameters)
    return {
        "_id": ObjectId(), "client": "ACME-UK", "source": "web", "locale": "en_GB",
        "identifiers": {"category": "Washer Dryer", "gteeLabour": "12"},
        "registrationParameters": params,
    }


def captured_assignment_input(monkeypatch, device):
    """Run the by-device wrapper over `device` and return the inputs it assembled."""
    devices = MagicMock()
    devices.find_one.return_value = device
    monkeypatch.setattr(assign_route, "devices_collection", devices)

    seen = {}

    def fake_assign(payload):
        seen["payload"] = payload
        return {
            "input": {"client": "ACME-UK", "source": "web", "category": "Washer Dryer",
                      "price": 449.99, "locale": "en_GB", "currency": "GBP"},
            "age_in_months": 12,
            "products": [{"productId": "REPAIR", "mode": "payment", "terms": [12]}],
        }

    monkeypatch.setattr(assign_route, "assign_products", fake_assign)
    assign_route.assign_product_for_device(str(device["_id"]))
    return seen["payload"]


@pytest.mark.parametrize("stored,expected", [
    ({"deviceWorking": False}, False),
    ({"deviceWorking": True}, True),
])
def test_assignment_reads_the_stored_flag(monkeypatch, stored, expected):
    payload = captured_assignment_input(monkeypatch, stored_device(**stored))
    assert payload.device_working is expected


def test_a_device_registered_before_the_field_existed_counts_as_working(monkeypatch):
    """No backfill: assignment for the devices already in Mongo must not change."""
    payload = captured_assignment_input(monkeypatch, stored_device())
    assert payload.device_working is True


# ---- the rule clause ----

def req(**over):
    base = dict(client="ACME-UK", source="web", category="Washer Dryer", price=400.0,
                locale="en_GB", purchase_date="2025-01-01", gtee=12, currency="GBP")
    base.update(over)
    return pa.ProductAssignmentRequest(**base)


def rule(when_extra=None):
    when = {"locale": ["en_GB"], "currency": ["GBP"], "guaranteeMonths": [12],
            "deviceAgeMonths": {"min": 0, "max": 120}, "price": {"min": 0, "max": 9999}}
    when.update(when_extra or {})
    return {"ruleId": "R", "when": when}


@pytest.mark.parametrize("device_working", [True, False])
def test_a_rule_that_says_nothing_takes_a_device_in_either_state(device_working):
    """Backwards compatibility: no existing rule carries `deviceWorking`."""
    assert pa.when_failure_reasons(rule(), req(device_working=device_working), 12) == []


def test_a_repair_rule_takes_only_a_broken_device():
    repair = rule({"deviceWorking": False})
    assert pa.when_failure_reasons(repair, req(device_working=False), 12) == []
    reasons = pa.when_failure_reasons(repair, req(device_working=True), 12)
    assert any("deviceWorking" in r for r in reasons), reasons


def test_a_cover_rule_can_exclude_a_broken_device():
    cover = rule({"deviceWorking": True})
    assert pa.when_failure_reasons(cover, req(device_working=True), 12) == []
    reasons = pa.when_failure_reasons(cover, req(device_working=False), 12)
    assert any("deviceWorking" in r for r in reasons), reasons


def test_the_request_defaults_to_working():
    """Assignment callers that quote a hypothetical device — the widget, for one — send
    nothing, and must keep matching the same rules."""
    assert req().device_working is True
