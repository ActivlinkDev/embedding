"""allow_manual: registering a device with no CustomSKU/MasterSKU match."""

from unittest.mock import MagicMock

import pytest
from bson import ObjectId

from routers import device_register as route
from utils import category_tree

TAXONOMY = {
    "Washer Dryer": {"category": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances"},
}


def seed_taxonomy(monkeypatch, taxonomy=TAXONOMY):
    from utils import category_tree
    import time

    folded = {category_tree._key(name): dict(entry) for name, entry in taxonomy.items()}
    monkeypatch.setattr(category_tree, "_load", lambda: dict(folded))
    monkeypatch.setattr(category_tree, "_cache", dict(folded), raising=False)
    monkeypatch.setattr(category_tree, "_cache_loaded_at", time.monotonic(), raising=False)


def mock_collections(monkeypatch, *, resolved=None):
    """Wire up a client/locale that always validate, no duplicates, and a catalogue
    resolver returning `resolved` (None means "no match", the default)."""
    clients = MagicMock()
    clients.find_one.return_value = {"Client_ID": "ACME-UK", "ClientKey": "acme_uk_live"}
    monkeypatch.setattr(route, "clients_collection", clients)

    locales = MagicMock()
    locales.find_one.return_value = {"locale": "en_GB", "currency": "GBP"}
    monkeypatch.setattr(route, "locale_params_collection", locales)

    devices = MagicMock()
    devices.find_one.return_value = None  # no duplicates
    inserted_id = ObjectId()
    devices.insert_one.return_value = MagicMock(inserted_id=inserted_id)
    monkeypatch.setattr(route, "devices_collection", devices)

    catalog = MagicMock()
    catalog.resolve_lookup.return_value = (resolved, None)
    monkeypatch.setattr(route, "catalog", catalog)

    return devices


def make_payload(**device_overrides):
    device = {
        "Identifiers": {"make": "Beko", "model": "WTB1000X1W"},
        "Unique_Parameters": {"serial": "SN-1", "purchase_date": "2025-05-01", "price": 449.99},
    }
    device.update(device_overrides)
    return route.SimpleRegisterRequest(
        clientkey="acme_uk_live",
        locale="en_GB",
        source="web",
        Devices=[route.DeviceModel(**device)],
    )


def test_allow_manual_stores_the_device_without_a_catalogue_match(monkeypatch):
    seed_taxonomy(monkeypatch)
    devices = mock_collections(monkeypatch, resolved=None)

    # Mirrors what the manual entry screen actually sends: the guarantee is asked
    # for there because assignment rejects a device carrying none.
    payload = make_payload(
        Identifiers={
            "make": "Beko", "model": "WTB1000X1W", "category": "washer-dryer",
            "gtee_labour": "24", "gtee_parts": "24",
        },
        allow_manual=True,
    )
    result = route.device_register(payload)

    assert result["count"] == 1
    assert result["inserted"][0]["skuStatus"] == "manual"
    assert "deviceId" in result["inserted"][0]

    stored = devices.insert_one.call_args.args[0]
    assert stored["skuStatus"] == "manual"
    assert stored["customSkuId"] is None
    assert stored["masterSkuId"] is None
    # Stored in the taxonomy's own spelling, not the caller's.
    assert stored["identifiers"]["category"] == "Washer Dryer"
    assert stored["identifiers"]["make"] == "Beko"
    assert stored["identifiers"]["model"] == "WTB1000X1W"
    # The values assignment reads off the stored device, so it can be quoted.
    assert stored["identifiers"]["gteeLabour"] == "24"
    assert stored["identifiers"]["gteeParts"] == "24"
    assert stored["registrationParameters"]["price"] == 449.99


def test_allow_manual_accepts_a_device_with_no_guarantee(monkeypatch):
    """Deliberate: registration is not quoting.

    A device with no guarantee cannot be assigned cover (ProductAssignment
    counts gtee 0 as missing), but refusing to register it would lose the
    registration outright instead of merely declining cover — and a
    catalogue-matched device whose MasterSKU carries no guarantee is accepted
    on exactly the same terms.
    """
    seed_taxonomy(monkeypatch)
    devices = mock_collections(monkeypatch, resolved=None)

    payload = make_payload(
        Identifiers={"make": "Beko", "model": "WTB1000X1W", "category": "Washer Dryer"},
        allow_manual=True,
    )
    result = route.device_register(payload)

    assert result["inserted"][0]["skuStatus"] == "manual"
    assert devices.insert_one.call_args.args[0]["identifiers"]["gteeLabour"] == ""


def test_allow_manual_fails_closed_when_the_taxonomy_is_unreadable(monkeypatch):
    """`validate_category` fails open so a Mongo blip cannot block SKU creation.

    A manual device is the opposite case: nothing else supplies its category, so
    an unverifiable one must not be stored — it would match nothing in
    assignment or rating, silently and permanently.
    """
    monkeypatch.setattr(category_tree, "_load", dict)
    monkeypatch.setattr(category_tree, "_cache", {}, raising=False)
    monkeypatch.setattr(category_tree, "_cache_loaded_at", 0.0, raising=False)
    assert category_tree.loaded() is False

    devices = mock_collections(monkeypatch, resolved=None)
    payload = make_payload(
        Identifiers={"make": "Beko", "model": "WTB1000X1W", "category": "Washer Dryer"},
        allow_manual=True,
    )
    result = route.device_register(payload)

    entry = result["inserted"][0]
    assert entry["skuStatus"] == "error"
    assert "deviceId" not in entry
    devices.insert_one.assert_not_called()


@pytest.mark.parametrize("category", ["", "Not A Real Category"])
def test_allow_manual_rejects_a_blank_or_unknown_category(monkeypatch, category):
    seed_taxonomy(monkeypatch)
    devices = mock_collections(monkeypatch, resolved=None)

    payload = make_payload(
        Identifiers={"make": "Beko", "model": "WTB1000X1W", "category": category},
        allow_manual=True,
    )
    result = route.device_register(payload)

    assert result["count"] == 1
    entry = result["inserted"][0]
    assert entry["skuStatus"] == "error"
    assert "deviceId" not in entry
    devices.insert_one.assert_not_called()


def test_allow_manual_does_not_block_the_rest_of_the_batch(monkeypatch):
    """A bad category on one device in a batch must not sink the others."""
    seed_taxonomy(monkeypatch)
    devices = mock_collections(monkeypatch, resolved=None)

    bad_device = route.DeviceModel(
        Identifiers={"make": "Beko", "model": "Bad", "category": "Nonesuch"},
        Unique_Parameters={"serial": "SN-BAD", "purchase_date": "2025-05-01", "price": 100},
        allow_manual=True,
    )
    good_device = route.DeviceModel(
        Identifiers={"make": "Beko", "model": "Good", "category": "Washer Dryer"},
        Unique_Parameters={"serial": "SN-GOOD", "purchase_date": "2025-05-01", "price": 100},
        allow_manual=True,
    )
    payload = route.SimpleRegisterRequest(
        clientkey="acme_uk_live", locale="en_GB", source="web",
        Devices=[bad_device, good_device],
    )

    result = route.device_register(payload)

    assert result["count"] == 2
    assert result["inserted"][0]["skuStatus"] == "error"
    assert result["inserted"][1]["skuStatus"] == "manual"
    devices.insert_one.assert_called_once()


def test_allow_manual_false_keeps_existing_behaviour(monkeypatch):
    """Regression guard: the default path is unchanged by this feature."""
    seed_taxonomy(monkeypatch)
    devices = mock_collections(monkeypatch, resolved=None)

    payload = make_payload(
        Identifiers={"make": "Beko", "model": "WTB1000X1W", "category": "Washer Dryer"},
    )  # allow_manual defaults to False
    result = route.device_register(payload)

    entry = result["inserted"][0]
    assert entry["skuStatus"] == "error"
    assert "CustomSKU or MasterSKU" in entry["detail"]
    devices.insert_one.assert_not_called()


def test_duplicate_detection_still_short_circuits_a_manual_device(monkeypatch):
    seed_taxonomy(monkeypatch)
    devices = mock_collections(monkeypatch, resolved=None)
    existing_id = ObjectId()
    devices.find_one.return_value = {
        "_id": existing_id,
        "identifiers": {"make": "Beko", "model": "WTB1000X1W"},
        "uniqueParameters": {"serial": "SN-1"},
    }

    payload = make_payload(
        Identifiers={"make": "Beko", "model": "WTB1000X1W", "category": "Washer Dryer"},
        allow_manual=True,
    )
    result = route.device_register(payload)

    entry = result["inserted"][0]
    assert entry["skuStatus"] == "duplicate record found"
    assert entry["deviceId"] == str(existing_id)
    devices.insert_one.assert_not_called()
