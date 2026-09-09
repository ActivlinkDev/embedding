from unittest.mock import MagicMock
from bson import ObjectId
from routers import registration_overview as route
import pytest
from fastapi import HTTPException


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
    result = route.my_registrations(route.MyRegistrationsRequest(phone='+447700900123'))
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


def test_receipt_requires_device_ownership(monkeypatch):
    devices = MagicMock()
    devices.find_one.return_value = None
    owner = {'registrationPhone': '+447700900123'}
    monkeypatch.setattr(route, 'registration_owner_query', lambda phone: owner)
    monkeypatch.setattr(route, 'devices_collection', devices)
    device_id = str(ObjectId())
    with pytest.raises(HTTPException) as error:
        route.registration_receipt(route.RegistrationReceiptRequest(phone='+447700900123', device_id=device_id))
    assert error.value.status_code == 404
    assert devices.find_one.call_args.args[0] == {'_id': ObjectId(device_id), **owner}


def test_receipt_returns_private_file(monkeypatch):
    devices = MagicMock()
    devices.find_one.return_value = {'receipt': {
        'name': 'purchase receipt.pdf', 'contentType': 'application/pdf', 'data': b'%PDF-1.7',
    }}
    monkeypatch.setattr(route, 'registration_owner_query', lambda phone: {'registrationPhone': phone})
    monkeypatch.setattr(route, 'devices_collection', devices)
    response = route.registration_receipt(route.RegistrationReceiptRequest(phone='+447700900123', device_id=str(ObjectId())))
    assert response.body == b'%PDF-1.7'
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['content-type'] == 'application/pdf'
    assert 'purchase%20receipt.pdf' in response.headers['content-disposition']
