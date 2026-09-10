"""Pairing devices to a customer on the journeys that never reach Stripe."""
from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from fastapi import HTTPException

from routers.customer import pair_customer_devices as route

PHONE = '+447700900123'


def request(customer_id, device_ids, phone=PHONE):
    return route.PairCustomerDevicesRequest(
        customer_id=customer_id, device_ids=device_ids, phone=phone)


@pytest.fixture
def collections(monkeypatch):
    customers = MagicMock()
    devices = MagicMock()
    customers.find_one.return_value = {'_id': ObjectId(), 'devices': []}
    devices.count_documents.side_effect = lambda query: len(query['_id']['$in'])
    devices.update_one.return_value.matched_count = 1
    devices.update_one.return_value.modified_count = 1
    monkeypatch.setattr(route, 'customer_collection', customers)
    monkeypatch.setattr(route, 'devices_collection', devices)
    return customers, devices


def test_device_is_assigned_to_the_customer_without_a_basket(collections):
    customers, devices = collections
    customer_id, device_id = str(ObjectId()), str(ObjectId())

    result = route.pair_customer_devices(request(customer_id, [device_id]))

    query, update = devices.update_one.call_args.args
    assert query['_id'] == ObjectId(device_id)
    assert update['$set']['registrationParameters.registrationStatus'] == 'assigned'
    assert update['$set']['registrationParameters.customerId'] == customer_id
    assert result['devices'] == [{'deviceId': device_id, 'status': 'registered'}]
    assert result['device_update'] == {'attempted': 1, 'matched': 1, 'modified': 1}
    pushed = customers.update_one.call_args.args[1]['$push']['devices']
    assert pushed == {'deviceId': device_id, 'status': 'registered'}


def test_customer_not_carrying_the_verified_phone_is_refused(collections):
    customers, devices = collections
    customers.find_one.return_value = None

    with pytest.raises(HTTPException) as error:
        route.pair_customer_devices(request(str(ObjectId()), [str(ObjectId())]))

    assert error.value.status_code == 403
    devices.update_one.assert_not_called()
    customers.update_one.assert_not_called()


def test_device_claimed_by_another_phone_is_refused(collections):
    customers, devices = collections
    devices.count_documents.side_effect = None
    devices.count_documents.return_value = 0

    with pytest.raises(HTTPException) as error:
        route.pair_customer_devices(request(str(ObjectId()), [str(ObjectId())]))

    assert error.value.status_code == 403
    devices.update_one.assert_not_called()
    customers.update_one.assert_not_called()


def test_device_paired_with_another_customer_is_skipped_not_reassigned(collections):
    customers, devices = collections
    devices.update_one.return_value.matched_count = 0
    devices.update_one.return_value.modified_count = 0
    device_id = str(ObjectId())

    result = route.pair_customer_devices(request(str(ObjectId()), [device_id]))

    assert result['devices'] == [{'deviceId': device_id, 'status': 'skipped',
                                  'reason': 'Already paired with another customer'}]
    # The customer's array is only touched for devices that actually paired.
    customers.update_one.assert_not_called()


def test_existing_contract_status_is_not_downgraded(collections):
    customers, devices = collections
    device_id = str(ObjectId())
    customers.find_one.return_value = {
        '_id': ObjectId(), 'devices': [{'deviceId': device_id, 'status': 'contract'}]}

    result = route.pair_customer_devices(request(str(ObjectId()), [device_id]))

    assert result['devices'] == [{'deviceId': device_id, 'status': 'contract'}]
    pushed = customers.update_one.call_args.args[1]['$push']['devices']
    assert pushed['status'] == 'contract'


def test_duplicate_device_ids_are_paired_once(collections):
    customers, devices = collections
    device_id = str(ObjectId())

    result = route.pair_customer_devices(request(str(ObjectId()), [device_id, device_id]))

    assert devices.update_one.call_count == 1
    assert result['device_update']['attempted'] == 1


@pytest.mark.parametrize('customer_id,device_ids', [
    ('not-an-objectid', [str(ObjectId())]),
    (str(ObjectId()), ['not-an-objectid']),
], ids=['bad-customer-id', 'bad-device-id'])
def test_invalid_identifiers_are_rejected_before_any_read(collections, customer_id, device_ids):
    customers, devices = collections

    with pytest.raises(HTTPException) as error:
        route.pair_customer_devices(request(customer_id, device_ids))

    assert error.value.status_code == 400
    customers.find_one.assert_not_called()
    devices.count_documents.assert_not_called()


def test_phone_pattern_does_not_match_a_longer_number():
    import re
    pattern = route.phone_pattern(PHONE)['$regex']
    assert re.match(pattern, '+44 7700 900123')
    assert not re.match(pattern, '+1447700900123')
    assert not re.match(pattern, '+4477009001234')
