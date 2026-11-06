"""Tenant scoping for `GET /devices/by-id` and `POST /pair-customer`.

Both reach devices by id, so naming a ClientKey is not what lets them in: a credential pinned to
one client must not read, or pair, a device another client registered. Ownership comes from the
device document's `client` (a Client_ID), never from anything the request supplies.
"""
from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from fastapi import HTTPException

from routers.customer import pair_customer as pc
from routers.devices import get_device_by_id as gd

CLIENT_KEY = "AO12345"
CLIENT_ID = "AO"


# --- GET /devices/by-id -----------------------------------------------------------------

def _devices(monkeypatch, doc):
    collection = MagicMock()
    collection.find_one.return_value = doc
    monkeypatch.setattr(gd, "devices_collection", collection)
    return collection


def test_pinned_caller_query_is_restricted_to_its_client(monkeypatch):
    device_id = str(ObjectId())
    collection = _devices(monkeypatch, {"_id": ObjectId(device_id), "client": CLIENT_ID})
    monkeypatch.setattr(gd, "client_id_for_key", lambda key: CLIENT_ID if key == CLIENT_KEY else None)

    result = gd.get_device_by_id(device_id, scope=CLIENT_KEY)

    assert result["data"]["_id"] == device_id
    assert collection.find_one.call_args.args[0] == {"_id": ObjectId(device_id), "client": CLIENT_ID}


def test_another_tenants_device_is_a_404(monkeypatch):
    # The client filter makes Mongo find nothing, exactly as for an unknown id.
    _devices(monkeypatch, None)
    monkeypatch.setattr(gd, "client_id_for_key", lambda key: CLIENT_ID)

    with pytest.raises(HTTPException) as exc:
        gd.get_device_by_id(str(ObjectId()), scope=CLIENT_KEY)
    assert exc.value.status_code == 404 and exc.value.detail == "Device not found"


def test_pinned_key_that_resolves_to_no_client_never_widens_the_query(monkeypatch):
    collection = _devices(monkeypatch, {"_id": ObjectId(), "client": "SOMEONE"})
    monkeypatch.setattr(gd, "client_id_for_key", lambda key: None)

    with pytest.raises(HTTPException) as exc:
        gd.get_device_by_id(str(ObjectId()), scope="RETIRED-KEY")
    assert exc.value.status_code == 404
    collection.find_one.assert_not_called()


def test_unrestricted_caller_is_unchanged(monkeypatch):
    device_id = str(ObjectId())
    collection = _devices(monkeypatch, {"_id": ObjectId(device_id), "client": "ANY"})

    gd.get_device_by_id(device_id, scope=None)

    assert collection.find_one.call_args.args[0] == {"_id": ObjectId(device_id)}


# --- POST /pair-customer ----------------------------------------------------------------

def _pairing(monkeypatch, device_ids):
    basket = {"_id": ObjectId(), "Basket": [{"deviceId": d} for d in device_ids]}
    baskets, customers, devices = MagicMock(), MagicMock(), MagicMock()
    baskets.find_one.return_value = basket
    customers.find_one.return_value = {"_id": ObjectId()}
    customers.update_one.return_value = MagicMock(matched_count=1, modified_count=1)
    devices.update_one.return_value = MagicMock(matched_count=1, modified_count=1)
    monkeypatch.setattr(pc, "basket_collection", baskets)
    monkeypatch.setattr(pc, "customer_collection", customers)
    monkeypatch.setattr(pc, "devices_collection", devices)
    monkeypatch.setattr(pc, "client_id_for_key", lambda key: CLIENT_ID if key == CLIENT_KEY else None)
    return str(basket["_id"]), customers, devices


def test_pinned_caller_pairs_its_own_devices_with_an_owner_filter(monkeypatch):
    ours = [str(ObjectId()), str(ObjectId())]
    basket_id, _, devices = _pairing(monkeypatch, ours)
    monkeypatch.setattr(pc, "device_ids_owned_by", lambda client_id, ids: set(ids))

    result = pc.pair_customer(str(ObjectId()), basket_id, scope=CLIENT_KEY)

    assert result["device_update"]["matched"] == 2
    for call in devices.update_one.call_args_list:
        assert call.args[0]["client"] == CLIENT_ID


def test_basket_holding_another_tenants_device_writes_nothing(monkeypatch):
    ours, theirs = str(ObjectId()), str(ObjectId())
    basket_id, customers, devices = _pairing(monkeypatch, [ours, theirs])
    monkeypatch.setattr(pc, "device_ids_owned_by", lambda client_id, ids: {ours})

    with pytest.raises(HTTPException) as exc:
        pc.pair_customer(str(ObjectId()), basket_id, scope=CLIENT_KEY)

    assert exc.value.status_code == 404 and exc.value.detail == "Basket not found"
    devices.update_one.assert_not_called()
    customers.update_one.assert_not_called()


def test_unknown_pinned_key_writes_nothing(monkeypatch):
    basket_id, customers, devices = _pairing(monkeypatch, [str(ObjectId())])

    with pytest.raises(HTTPException) as exc:
        pc.pair_customer(str(ObjectId()), basket_id, scope="RETIRED-KEY")

    assert exc.value.status_code == 404
    devices.update_one.assert_not_called()
    customers.update_one.assert_not_called()


def test_unrestricted_caller_pairing_is_unchanged(monkeypatch):
    device_id = str(ObjectId())
    basket_id, _, devices = _pairing(monkeypatch, [device_id])

    pc.pair_customer(str(ObjectId()), basket_id, scope=None)

    assert devices.update_one.call_args.args[0] == {"_id": ObjectId(device_id)}
