from unittest.mock import MagicMock
from bson import ObjectId
from routers import registration_overview as route
import pytest
from fastapi import HTTPException


CLIENT_KEY = 'AO12345'
CLIENT_ID = 'AO'


@pytest.fixture(autouse=True)
def resolvable_client(monkeypatch):
    """Default: CLIENT_KEY resolves to CLIENT_ID, every other key is unknown."""
    monkeypatch.setattr(route, 'client_id_for_key',
                        lambda key: CLIENT_ID if key == CLIENT_KEY else None)


def test_lookup_is_scoped_to_phone_and_matching_customers(monkeypatch):
    customer_id, device_id, master_id = ObjectId(), ObjectId(), ObjectId()
    client = MagicMock()
    db = client.__getitem__.return_value
    db.__getitem__.return_value.find.return_value = [{'_id': customer_id}]
    db.__getitem__.return_value.find_one.return_value = {'imageUrl': 'https://example.com/device.jpg'}
    devices = MagicMock()
    devices.find.return_value.sort.return_value = [{
        '_id': device_id, 'masterSkuId': str(master_id), 'locale': 'en-GB',
        'registrationParameters': {'customerId': str(customer_id)},
        'receipt': {'data': b'private'}, 'registrationPhone': '+447700900123',
    }]
    monkeypatch.setattr(route, 'client', client)
    monkeypatch.setattr(route, 'devices_collection', devices)
    result = route.my_registrations(
        route.MyRegistrationsRequest(phone='+447700900123', clientkey=CLIENT_KEY))
    query, projection = devices.find.call_args.args
    import re
    pattern = query['$or'][0]['registrationPhone']['$regex']
    assert re.fullmatch(pattern, '+44 (7700) 900123')
    assert not re.fullmatch(pattern, '+447700900124')
    assert query['$or'][1]['registrationParameters.customerId']['$in'] == [customer_id, str(customer_id)]
    assert projection == {'receipt.data': 0}
    assert result['devices'][0]['imageUrl'] == 'https://example.com/device.jpg'
    assert 'data' not in result['devices'][0]['receipt']
    assert 'registrationPhone' not in result['devices'][0]
    assert 'customerId' not in result['devices'][0]['registrationParameters']
    devices.update_many.assert_not_called()


def test_lookup_is_scoped_to_the_requesting_client(monkeypatch):
    """A customer who registered with two brands sees only the one whose subdomain
    they are on: without the `client` clause, ao.* would list Beko registrations."""
    client = MagicMock()
    db = client.__getitem__.return_value
    db.__getitem__.return_value.find.return_value = []
    devices = MagicMock()
    devices.find.return_value.sort.return_value = []
    monkeypatch.setattr(route, 'client', client)
    monkeypatch.setattr(route, 'devices_collection', devices)

    route.my_registrations(route.MyRegistrationsRequest(phone='+447700900123', clientkey=CLIENT_KEY))

    query = devices.find.call_args.args[0]
    # ANDed with ownership, so proving the phone alone never crosses the tenant line.
    assert query['client'] == CLIENT_ID
    assert '$or' in query


def test_lookup_rejects_an_unknown_clientkey(monkeypatch):
    """An unknown key must fail rather than fall through to an unscoped query."""
    devices = MagicMock()
    monkeypatch.setattr(route, 'devices_collection', devices)
    with pytest.raises(HTTPException) as error:
        route.my_registrations(
            route.MyRegistrationsRequest(phone='+447700900123', clientkey='NOT-A-CLIENT'))
    assert error.value.status_code == 400
    devices.find.assert_not_called()


def test_lookup_requires_a_clientkey():
    """The tenant is mandatory: the OTP proves the customer, never the storefront."""
    with pytest.raises(Exception):
        route.MyRegistrationsRequest(phone='+447700900123')


def test_receipt_requires_device_ownership(monkeypatch):
    devices = MagicMock()
    devices.find_one.return_value = None
    owner = {'client': CLIENT_ID, 'registrationPhone': '+447700900123'}
    monkeypatch.setattr(route, 'registration_owner_query', lambda phone, client_id: owner)
    monkeypatch.setattr(route, 'devices_collection', devices)
    device_id = str(ObjectId())
    with pytest.raises(HTTPException) as error:
        route.registration_receipt(route.RegistrationReceiptRequest(
            phone='+447700900123', device_id=device_id, clientkey=CLIENT_KEY))
    assert error.value.status_code == 404
    assert devices.find_one.call_args.args[0] == {'_id': ObjectId(device_id), **owner}


def test_receipt_is_scoped_to_the_requesting_client(monkeypatch):
    """Another tenant's receipt stays unreachable even with a valid device id."""
    devices = MagicMock()
    devices.find_one.return_value = None
    monkeypatch.setattr(route, 'devices_collection', devices)
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value.find.return_value = []
    monkeypatch.setattr(route, 'client', client)
    with pytest.raises(HTTPException):
        route.registration_receipt(route.RegistrationReceiptRequest(
            phone='+447700900123', device_id=str(ObjectId()), clientkey=CLIENT_KEY))
    assert devices.find_one.call_args.args[0]['client'] == CLIENT_ID


def test_receipt_returns_private_file(monkeypatch):
    devices = MagicMock()
    devices.find_one.return_value = {'receipt': {
        'name': 'purchase receipt.pdf', 'contentType': 'application/pdf', 'data': b'%PDF-1.7',
    }}
    monkeypatch.setattr(route, 'registration_owner_query',
                        lambda phone, client_id: {'registrationPhone': phone})
    monkeypatch.setattr(route, 'devices_collection', devices)
    response = route.registration_receipt(route.RegistrationReceiptRequest(
        phone='+447700900123', device_id=str(ObjectId()), clientkey=CLIENT_KEY))
    assert response.body == b'%PDF-1.7'
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['content-type'] == 'application/pdf'
    assert 'purchase%20receipt.pdf' in response.headers['content-disposition']
