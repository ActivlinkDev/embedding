"""Per-client marketing consent on the customer record.

Consent is held per ClientKey because one customer can be reached through several clients,
each with its own opt-in. These tests pin the two rules that follow from that: the latest
submission for a client replaces that client's entry, and no other client's entry is touched.
"""
from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from fastapi import HTTPException

from routers.customer import create_customer as route

CLIENT_KEY = 'acme_uk_live'
OTHER_CLIENT_KEY = 'globex_fr_live'
NAME = 'Jane Okafor'
PHONE = '+447700900123'
EMAIL = 'jane.okafor@example.com'
ALL_ON = {'email': True, 'sms': True, 'phone': True, 'post': True}


@pytest.fixture
def collections(monkeypatch):
    """A customer collection that matches an existing customer, and a known ClientKey."""
    customers = MagicMock()
    customer_id = ObjectId()
    customers.find_one.return_value = {'_id': customer_id}
    # Default: the customer carries no entry for this client yet, so the replace finds
    # nothing and the append is what applies.
    customers.update_one.side_effect = [
        MagicMock(matched_count=0),
        MagicMock(matched_count=1),
    ]

    clientkeys = MagicMock()
    clientkeys.find_one.return_value = {'ClientKey': CLIENT_KEY, 'Client_ID': 'ACME-UK'}

    monkeypatch.setattr(route, 'customer_collection', customers)
    monkeypatch.setattr(route, 'clientkey_collection', clientkeys)
    return customers, clientkeys, customer_id


def call(collections, **overrides):
    payload = {
        'name': NAME,
        'telephone': PHONE,
        'email': EMAIL,
        'clientKey': CLIENT_KEY,
        'marketingPreferences': ALL_ON,
    }
    payload.update(overrides)
    return route.get_or_create_customer_endpoint(**payload)


# --- normalisation -------------------------------------------------------------------

def test_omitted_channels_are_stored_as_an_explicit_opt_out():
    # A caller sending only the channels the customer ticked must not leave the rest
    # ambiguous — an unticked box is a "no", not an absence.
    assert route.normalise_marketing_preferences({'email': True}) == {
        'email': True, 'sms': False, 'phone': False, 'post': False,
    }


def test_unknown_channels_are_dropped_rather_than_stored():
    result = route.normalise_marketing_preferences({'email': True, 'carrier_pigeon': True})

    assert 'carrier_pigeon' not in result
    assert set(result) == set(route.MARKETING_CHANNELS)


def test_a_missing_payload_is_distinguishable_from_opting_everything_out():
    # None means "leave stored consent alone"; an all-false dict means "opt me out".
    assert route.normalise_marketing_preferences(None) is None
    assert route.normalise_marketing_preferences('email') is None
    assert route.normalise_marketing_preferences({}) == {
        'email': False, 'sms': False, 'phone': False, 'post': False,
    }


# --- storage -------------------------------------------------------------------------

def test_consent_is_appended_for_a_client_the_customer_has_none_for(collections):
    customers, _, customer_id = collections

    result = call(collections)

    assert result['marketingPreferencesStored'] is True
    replace_query, replace_update = customers.update_one.call_args_list[0].args
    assert replace_query['marketingPreferences.clientKey'] == CLIENT_KEY
    assert replace_update['$set']['marketingPreferences.$']['channels'] == ALL_ON

    append_query, append_update = customers.update_one.call_args_list[1].args
    # The guard is what stops a concurrent request producing a second entry for this client.
    assert append_query['marketingPreferences.clientKey'] == {'$ne': CLIENT_KEY}
    assert append_update['$push']['marketingPreferences']['clientKey'] == CLIENT_KEY
    assert append_query['_id'] == customer_id


def test_existing_consent_for_the_same_client_is_replaced_not_duplicated(collections):
    customers, _, _ = collections
    customers.update_one.side_effect = [MagicMock(matched_count=1)]

    latest = {'email': False, 'sms': True, 'phone': False, 'post': False}
    result = call(collections, marketingPreferences=latest)

    assert result['marketingPreferencesStored'] is True
    # One update only: the entry was replaced in place, so nothing was appended.
    assert customers.update_one.call_count == 1
    _, update = customers.update_one.call_args.args
    assert update['$set']['marketingPreferences.$']['channels'] == latest


def test_writing_one_clients_consent_never_touches_another_clients(collections):
    customers, _, _ = collections

    call(collections)

    for call_args in customers.update_one.call_args_list:
        query, update = call_args.args
        # Every write is addressed by this ClientKey, and none rewrites the whole array.
        assert CLIENT_KEY in str(query)
        assert OTHER_CLIENT_KEY not in str(query)
        assert 'marketingPreferences' not in update.get('$set', {})


def test_an_append_that_loses_its_race_is_retried_as_a_replace(collections):
    customers, _, _ = collections
    # Replace misses, append loses to a concurrent request, then the replace applies.
    customers.update_one.side_effect = [
        MagicMock(matched_count=0),
        MagicMock(matched_count=0),
        MagicMock(matched_count=1),
    ]

    result = call(collections)

    assert result['marketingPreferencesStored'] is True
    assert customers.update_one.call_count == 3


def test_consent_is_stored_for_a_customer_who_already_existed(collections):
    # A returning customer re-submitting the form is the main path for "latest wins";
    # get-or-create otherwise leaves a matched record untouched.
    customers, _, _ = collections
    customers.find_one.return_value = {'_id': ObjectId()}

    result = call(collections)

    assert result['existing'] is True
    assert result['marketingPreferencesStored'] is True


# --- tenancy and guards ---------------------------------------------------------------

def test_an_unknown_client_key_is_refused_before_any_write(collections):
    customers, clientkeys, _ = collections
    clientkeys.find_one.return_value = None

    with pytest.raises(HTTPException) as raised:
        call(collections, clientKey='not-a-tenant')

    assert raised.value.status_code == 400
    customers.insert_one.assert_not_called()
    customers.update_one.assert_not_called()


def test_preferences_without_a_client_key_are_ignored(collections):
    # There is no client to attribute the consent to, so it is dropped rather than
    # filed against an arbitrary tenant.
    customers, _, _ = collections

    result = call(collections, clientKey=None)

    assert result['marketingPreferencesStored'] is False
    customers.update_one.assert_not_called()


def test_omitting_preferences_leaves_stored_consent_alone(collections):
    customers, _, _ = collections

    result = call(collections, marketingPreferences=None)

    assert result['marketingPreferencesStored'] is False
    customers.update_one.assert_not_called()


def test_the_existing_response_contract_is_unchanged(collections):
    result = call(collections)

    assert result['customerId'] == str(collections[2])
    assert result['existing'] is True
