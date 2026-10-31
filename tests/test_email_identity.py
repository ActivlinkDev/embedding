"""Email-verified access to /my-registrations and /my-contracts, deep-link highlights,
/register creating Devices, and the customer email match."""
import asyncio
import os
from datetime import timedelta

os.environ.setdefault("EMAIL_OTP_SECRET", "o" * 40)

import pytest  # noqa: E402
from bson import ObjectId  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from routers import email_otp, embedded_register_device as register_route, registration_overview  # noqa: E402
from routers.comms import links, otp_codes, store  # noqa: E402
from routers.contract import admin as contract_admin  # noqa: E402
from routers.customer import create_customer  # noqa: E402
from routers.devices.registration_core import find_duplicate  # noqa: E402
from tests.fake_mongo import FakeCollection  # noqa: E402

KEYS = {"AO12345": "AO", "BEKO1": "BEKO"}
EMAIL = "paul.smith+home@gmail.com"


@pytest.fixture
def db(monkeypatch):
    customer = ObjectId()
    other_customer = ObjectId()
    device = ObjectId()
    beko_device = ObjectId()
    contract = ObjectId()
    customers = FakeCollection([
        {"_id": customer, "name": "Paul Smith", "email": EMAIL, "telephone": "+447700900123"},
        # Same characters with the regex specials acting as wildcards: must never match.
        {"_id": other_customer, "name": "Someone Else", "email": "paulxsmithhhome@gmail.com"},
    ])
    devices = FakeCollection([
        {"_id": device, "client": "AO", "registrationParameters": {"customerId": str(customer)},
         "identifiers": {"title": "Washer"}},
        {"_id": beko_device, "client": "BEKO", "registrationParameters": {"customerId": str(customer)},
         "identifiers": {"title": "Fridge"}},
    ])
    contracts = FakeCollection([
        {"_id": contract, "customer_id": str(customer), "device_id": str(device), "created_at": 2},
        {"_id": ObjectId(), "customer_id": str(customer), "device_id": str(beko_device), "created_at": 1},
    ])
    for name, col in {"customers": customers, "devices": devices, "contracts": contracts,
                      "links": FakeCollection(unique=["token_hash"]), "email_otp": FakeCollection(),
                      "email_log": FakeCollection(unique=["dedupe_key"])}.items():
        monkeypatch.setattr(store, name, col)
    monkeypatch.setattr(create_customer, "customer_collection", customers)
    owned = lambda client_id, ids: {d for d in ids if any(  # noqa: E731
        str(x["_id"]) == d and x["client"] == client_id for x in devices.docs)}
    for module in (email_otp, contract_admin):
        monkeypatch.setattr(module, "client_id_for_key", lambda key: KEYS.get(key))
        monkeypatch.setattr(module, "device_ids_owned_by", owned)
    monkeypatch.setattr(registration_overview, "client_id_for_key", lambda key: KEYS.get(key))

    class SortableCursor(list):
        def sort(self, *args):
            return self

    monkeypatch.setattr(contract_admin.svc, "contracts_collection",
                        MagicMock(find=lambda q, *a: SortableCursor(contracts.find(q))))
    monkeypatch.setattr(registration_overview, "devices_collection",
                        MagicMock(find=lambda q, *a: SortableCursor(devices.find(q)),
                                  find_one=lambda q, *a: devices.find_one(q)))
    monkeypatch.setattr(registration_overview, "client", MagicMock())

    sent = []

    async def fake_send(client_key, locale, email, code):
        sent.append({"client_key": client_key, "email": email, "code": code})
        return {"status": "sent"}

    monkeypatch.setattr(email_otp, "send_otp_email", fake_send)
    return {"customer": str(customer), "device": str(device), "beko_device": str(beko_device),
            "contract": str(contract), "sent": sent}


def request_code(**kw):
    return asyncio.run(email_otp.request_email_code(email_otp.EmailOtpRequest(clientkey="AO12345", **kw)))


# --- customer email match ------------------------------------------------------

def test_customer_email_match_is_literal_not_a_pattern(db):
    ids = create_customer.customer_ids_for_email(EMAIL.upper())
    assert db["customer"] in ids and len(ids) == 2  # ObjectId and str of the one customer


def test_blank_contact_fields_never_match_other_customers():
    col = FakeCollection([{"_id": ObjectId(), "telephone": "", "email": ""}])
    _id, existing = create_customer.get_or_create_customer(col, "New", "", "new@example.com")
    assert existing is False


# --- sending codes ---------------------------------------------------------------

def test_code_is_sent_only_to_a_customer_with_records_on_this_client(db):
    assert request_code(email=EMAIL) == {"sent": True, "maskedEmail": "p***@g***.com"}
    assert len(db["sent"]) == 1 and db["sent"][0]["client_key"] == "AO12345"

    unknown = request_code(email="stranger@example.com")
    assert unknown == {"sent": True, "maskedEmail": "s***@e***.com"}
    assert len(db["sent"]) == 1  # same answer, nothing emailed


def test_customer_of_another_client_gets_no_code(db, monkeypatch):
    store.devices.docs = [d for d in store.devices.docs if d["client"] == "BEKO"]
    store.contracts.docs = [c for c in store.contracts.docs if c["device_id"] == db["beko_device"]]
    request_code(email=EMAIL)
    assert db["sent"] == []


def test_sixth_request_in_an_hour_is_limited_for_registered_and_unknown_addresses(db):
    for email in (EMAIL, "stranger@example.com"):
        for _ in range(otp_codes.OTP_MAX_PER_HOUR):
            request_code(email=email)
        with pytest.raises(HTTPException) as err:
            request_code(email=email)
        assert err.value.status_code == 429


def test_link_addresses_the_code_on_its_own_tenant_only(db):
    token = links.create_link("device", db["device"], "AO", "AO12345", EMAIL, "en_GB")
    assert request_code(link=token)["maskedEmail"] == "p***@g***.com"
    assert db["sent"][0]["email"] == EMAIL

    beko = asyncio.run(email_otp.request_email_code(email_otp.EmailOtpRequest(clientkey="BEKO1", link=token)))
    assert beko == {"sent": True, "maskedEmail": ""} and len(db["sent"]) == 1


# --- verifying codes -------------------------------------------------------------

def verify(code, **kw):
    return email_otp.verify_email_code(email_otp.EmailOtpVerify(clientkey="AO12345", code=code, **kw))


def test_correct_code_verifies_once(db):
    request_code(email=EMAIL)
    code = db["sent"][0]["code"]
    assert verify(code, email=EMAIL) == {"verified": True, "email": EMAIL}
    with pytest.raises(HTTPException) as err:
        verify(code, email=EMAIL)
    assert err.value.status_code == 400


def test_code_through_a_link_returns_the_verified_address(db):
    token = links.create_link("device", db["device"], "AO", "AO12345", EMAIL, None)
    request_code(link=token)
    assert verify(db["sent"][0]["code"], link=token)["email"] == EMAIL


def test_wrong_codes_lock_out_after_five_attempts(db):
    request_code(email=EMAIL)
    code = db["sent"][0]["code"]
    wrong = "000000" if code != "000000" else "111111"
    statuses = []
    for _ in range(5):
        with pytest.raises(HTTPException) as err:
            verify(wrong, email=EMAIL)
        statuses.append(err.value.status_code)
    assert statuses == [400, 400, 400, 400, 429]
    with pytest.raises(HTTPException) as err:
        verify(code, email=EMAIL)
    assert err.value.status_code == 429


def test_a_correct_code_is_refused_once_the_attempts_are_spent(db):
    """Every comparison spends an attempt first, so a burst of parallel guesses (simulated
    by attempts already at the limit when the right code arrives) cannot get a sixth try."""
    request_code(email=EMAIL)
    store.email_otp.docs[-1]["attempts"] = otp_codes.OTP_MAX_ATTEMPTS
    with pytest.raises(HTTPException) as err:
        verify(db["sent"][0]["code"], email=EMAIL)
    assert err.value.status_code == 429


def test_expired_code_is_refused(db):
    request_code(email=EMAIL)
    store.email_otp.docs[-1]["expires_at"] = store.now() - timedelta(seconds=1)
    with pytest.raises(HTTPException) as err:
        verify(db["sent"][0]["code"], email=EMAIL)
    assert err.value.status_code == 400


# --- lookups and highlights -----------------------------------------------------

def test_my_registrations_by_email_is_scoped_to_the_client_and_highlights_owned_devices(db):
    token = links.create_link("device", db["device"], "AO", "AO12345", EMAIL, None)
    out = registration_overview.my_registrations(registration_overview.MyRegistrationsRequest(
        email=EMAIL, clientkey="AO12345", link=token))
    assert [d["deviceId"] for d in out["devices"]] == [db["device"]]
    assert out["highlightId"] == db["device"]


def test_highlight_needs_the_record_in_the_verified_results(db):
    # A link to a device this email does not own highlights nothing.
    token = links.create_link("device", str(ObjectId()), "AO", "AO12345", EMAIL, None)
    out = registration_overview.my_registrations(registration_overview.MyRegistrationsRequest(
        email=EMAIL, clientkey="AO12345", link=token))
    assert out["highlightId"] is None


def test_my_registrations_requires_exactly_one_identity():
    with pytest.raises(ValueError):
        registration_overview.MyRegistrationsRequest(clientkey="AO12345")
    with pytest.raises(ValueError):
        registration_overview.MyRegistrationsRequest(phone="+447700900123", email=EMAIL, clientkey="AO12345")


def test_my_contracts_by_email_is_scoped_and_highlights(db):
    token = links.create_link("contract", db["contract"], "AO", "AO12345", EMAIL, None)
    out = contract_admin.my_contracts_by_email(
        contract_admin.MyContractsRequest(email=EMAIL, clientkey="AO12345", link=token), scope=None)
    assert [c["id"] for c in out["contracts"]] == [db["contract"]]
    assert out["highlightId"] == db["contract"]

    none = contract_admin.my_contracts_by_email(
        contract_admin.MyContractsRequest(email="stranger@example.com", clientkey="AO12345"), scope=None)
    assert none == {"contracts": [], "highlightId": None}


def test_link_resolve_is_tenant_bound(db):
    token = links.create_link("contract", db["contract"], "AO", "AO12345", EMAIL, None)
    out = email_otp.resolve_email_link(email_otp.LinkResolveRequest(clientkey="AO12345", link=token))
    assert out == {"kind": "contract", "refId": db["contract"], "maskedEmail": "p***@g***.com"}
    with pytest.raises(HTTPException) as err:
        email_otp.resolve_email_link(email_otp.LinkResolveRequest(clientkey="BEKO1", link=token))
    assert err.value.status_code == 404


# --- /register creates Devices ---------------------------------------------------

def test_find_duplicate_checks_imei_first():
    col = FakeCollection([
        {"_id": "by-serial", "identifiers": {"make": "Bosch", "model": "X"}, "uniqueParameters": {"serial": "S1"}},
        {"_id": "by-imei", "uniqueParameters": {"imei": "356938035643809"}},
    ])
    hit = find_duplicate(col, make="Bosch", model="X", serial="S1", imei="356938035643809")
    assert hit["device"]["_id"] == "by-imei" and hit["matched_field"] == "imei"
    assert find_duplicate(col, make="Bosch", model="X", serial="S1")["matched_field"] == "make/model/serial"
    assert find_duplicate(col, make="string", model="", serial="S1") is None


@pytest.fixture
def register_env(monkeypatch):
    devices = FakeCollection([{"_id": ObjectId(), "client": "AO", "uniqueParameters": {"imei": "111111111111111"},
                               "registrationParameters": {"customerId": "first-owner"}}])
    customers = FakeCollection()
    clients = MagicMock(find_one=lambda q: {"ClientKey": "AO12345", "Client_ID": "AO"})
    locales = MagicMock(find_one=lambda q: {"locale": "en_GB", "currency": "GBP"})
    catalog = MagicMock()
    catalog.resolve_lookup.return_value = ({"product": {"title": "Bosch Washer", "category": "Laundry",
                                                        "guarantee": {"partsMonths": "24"}},
                                            "customSkuId": "c1", "masterSkuId": "m1"}, None)
    for name, value in {"devices_collection": devices, "customer_collection": customers,
                        "clients_collection": clients, "locale_params_collection": locales,
                        "catalog": catalog, "registrations_collection": FakeCollection(),
                        "registrations_error_log_collection": FakeCollection()}.items():
        monkeypatch.setattr(register_route, name, value)
    monkeypatch.setattr(register_route, "generate_qr_code", lambda url: "qr")
    return devices, customers


def register(devices_payload, customer=None):
    body = {"clientkey": "AO12345", "locale": "en_GB", "source": "web", "Devices": devices_payload}
    if customer is not None:
        body["customer"] = customer
    from fastapi import BackgroundTasks
    tasks = BackgroundTasks()
    return register_route.register(register_route.RegisterRequest(**body), tasks), tasks


def device(**unique):
    return {"Identifiers": {"make": "Bosch", "model": "WGG", "gtee_labour": "12"},
            "Unique_Parameters": {"serial": "SN-1", "purchase_date": "2026-10-01", "price": 449.99, **unique}}


def test_register_stores_a_device_and_assigns_the_customer(register_env):
    devices, customers = register_env
    out, tasks = register([device()], customer={"name": "Jane Okafor", "email": "jane@example.com",
                                                "phone": "+447700900111", "Opt_email": True})
    entry = out["devices"][0]
    assert entry["duplicate"] is False and entry["status"] == "matched"
    assert {"device_id", "Identifiers", "Unique_Parameters", "customSKU_id", "masterSKU_id"} <= entry.keys()
    assert {"status", "registration_id", "activation_code", "registration_url", "registration_qr"} <= out.keys()

    stored = devices.find_one({"_id": ObjectId(entry["deviceId"])})
    assert stored["client"] == "AO" and stored["skuStatus"] == "matched"
    assert stored["identifiers"]["title"] == "Bosch Washer" and stored["identifiers"]["gteeLabour"] == "12"
    assert stored["registrationParameters"]["currency"] == "GBP"
    assert stored["registrationParameters"]["price"] == 449.99
    customer = customers.docs[0]
    assert stored["registrationParameters"]["customerId"] == str(customer["_id"])
    assert stored["registrationParameters"]["registrationStatus"] == "assigned"
    assert "registrationPhone" not in stored  # unverified phone never claims ownership
    assert customer["marketingPreferences"][0]["channels"]["email"] is True
    assert tasks.tasks[0].args == (str(customer["_id"]), [entry["deviceId"]])


def test_register_duplicate_inserts_nothing_and_keeps_its_owner(register_env):
    devices, customers = register_env
    existing = devices.docs[0]
    out, tasks = register([device(imei="111111111111111")], customer={"email": "jane@example.com"})
    entry = out["devices"][0]
    assert entry == {**entry, "deviceId": str(existing["_id"]), "duplicate": True}
    assert len(devices.docs) == 1
    assert devices.docs[0]["registrationParameters"]["customerId"] == "first-owner"
    assert customers.docs == [] and tasks.tasks == []


def test_register_without_contact_details_creates_no_customer(register_env):
    devices, customers = register_env
    out, tasks = register([device()], customer={"name": "Anonymous"})
    assert len(devices.docs) == 2 and customers.docs == [] and tasks.tasks == []
    assert devices.docs[1]["registrationParameters"]["registrationStatus"] == "unassigned"
