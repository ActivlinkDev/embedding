"""Tenant scoping for the customer hub's contracts tab.

The customer hub verifies a phone number, which identifies the customer but says
nothing about which client's storefront they are on. A customer who bought cover
from two clients has contracts under both, so `GET /customers/{id}/contracts` has
to be told the tenant — otherwise ao.registermyproduct.io lists Beko contracts.
"""
from unittest.mock import MagicMock

from bson import ObjectId

from routers.contract import admin


CLIENT_KEY = 'AO12345'
CLIENT_ID = 'AO'


def _contracts(monkeypatch, docs):
    svc = MagicMock()
    svc.contracts_collection.find.return_value.sort.return_value = docs
    monkeypatch.setattr(admin, 'svc', svc)
    return svc


def test_contracts_are_filtered_to_devices_the_client_owns(monkeypatch):
    ours, theirs = str(ObjectId()), str(ObjectId())
    _contracts(monkeypatch, [
        {'_id': ObjectId(), 'device_id': ours, 'customer_id': 'c1'},
        {'_id': ObjectId(), 'device_id': theirs, 'customer_id': 'c1'},
    ])
    monkeypatch.setattr(admin, 'client_id_for_key', lambda key: CLIENT_ID)
    # Only `ours` comes back from the Devices lookup: `theirs` belongs to another client.
    monkeypatch.setattr(admin, 'device_ids_owned_by', lambda client_id, ids: {ours})

    result = admin.customer_contracts('c1', clientkey=CLIENT_KEY, scope=None)

    assert [d['device_id'] for d in result] == [ours]


def test_contracts_without_a_device_are_dropped(monkeypatch):
    """An unattributable contract must not surface on a client's storefront."""
    _contracts(monkeypatch, [{'_id': ObjectId(), 'customer_id': 'c1'},
                             {'_id': ObjectId(), 'device_id': '', 'customer_id': 'c1'}])
    monkeypatch.setattr(admin, 'client_id_for_key', lambda key: CLIENT_ID)
    monkeypatch.setattr(admin, 'device_ids_owned_by', lambda client_id, ids: set())

    assert admin.customer_contracts('c1', clientkey=CLIENT_KEY, scope=None) == []


def test_unknown_clientkey_returns_nothing(monkeypatch):
    """An unknown key must not fall back to every tenant's contracts."""
    _contracts(monkeypatch, [{'_id': ObjectId(), 'device_id': str(ObjectId()), 'customer_id': 'c1'}])
    monkeypatch.setattr(admin, 'client_id_for_key', lambda key: None)

    assert admin.customer_contracts('c1', clientkey='NOT-A-CLIENT', scope=None) == []


def test_omitting_clientkey_keeps_the_caller_scoped_behaviour(monkeypatch):
    """Internal/admin callers that never pass a clientkey are unchanged: they still
    get the caller's own tenant scope applied by `_scoped`."""
    device_id = str(ObjectId())
    svc = _contracts(monkeypatch, [{'_id': ObjectId(), 'device_id': device_id, 'customer_id': 'c1'}])

    result = admin.customer_contracts('c1', clientkey=None, scope='ACME')

    assert [d['device_id'] for d in result] == [device_id]
    assert svc.contracts_collection.find.call_args.args[0]['client_key'] == 'ACME'
