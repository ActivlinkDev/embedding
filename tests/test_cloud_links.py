"""Cloud ID page API: tenant isolation, OTP limits and the access grant."""
import os
import time
from datetime import timedelta

os.environ.setdefault("CLOUD_ACCESS_SECRET", "c" * 40)

import pytest  # noqa: E402
from bson import ObjectId  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from routers import cloud  # noqa: E402
from routers.comms import cloud_links, store  # noqa: E402
from tests.fake_mongo import FakeCollection  # noqa: E402

KEYS = {"AO12345": "AO", "BEKO1": "BEKO"}


@pytest.fixture
def db(monkeypatch):
    customer_id = ObjectId()
    device_id = ObjectId()
    cols = {
        "devices": FakeCollection([{
            "_id": device_id, "client": "AO", "registeredAt": "2026-10-30T10:00:00Z",
            "identifiers": {"title": "Washing machine", "make": "Bosch", "model": "WGG", "category": "Laundry"},
            "uniqueParameters": {"serial": "SN-8841203", "imei": "356938035643809"},
            "registrationParameters": {"customerId": str(customer_id), "price": 449.99, "currency": "GBP",
                                       "purchaseDate": "2026-10-01"},
            "registrationPhone": "+447700900123",
        }]),
        "customers": FakeCollection([{"_id": customer_id, "name": "Paul Smith", "telephone": "+447700900123"}]),
        "contracts": FakeCollection(),
        "baskets": FakeCollection(),
        "registrations": FakeCollection(),
        "cloud_links": FakeCollection(unique=["token_hash"]),
        "cloud_otp": FakeCollection(),
    }
    for name, col in cols.items():
        monkeypatch.setattr(store, name, col)
    monkeypatch.setattr(cloud, "client_id_for_key", lambda key: KEYS.get(key))
    token = cloud_links.create_link("device", str(device_id), "AO", "AO12345", "paul@example.com", "en_GB")
    return {"token": token, "device_id": str(device_id)}


def _status(fn, *args, **kwargs):
    with pytest.raises(HTTPException) as err:
        fn(*args, **kwargs)
    return err.value.status_code


def test_summary_has_no_personal_data(db):
    out = cloud.cloud_summary(db["token"], clientkey="AO12345")

    assert out["maskedEmail"] == "p***@e***.com"
    assert out["items"][0]["title"] == "Washing machine"
    text = repr(out)
    for secret in ("SN-8841203", "3809", "449.99", "Paul", "paul@example.com", "+4477"):
        assert secret not in text


def test_other_tenant_unknown_and_revoked_tokens_are_all_404(db):
    assert _status(cloud.cloud_summary, db["token"], clientkey="BEKO1") == 404
    assert _status(cloud.cloud_summary, db["token"], clientkey="NOPE") == 404
    assert _status(cloud.cloud_summary, "x" * 32, clientkey="AO12345") == 404
    store.cloud_links.docs[0]["revoked"] = True
    assert _status(cloud.cloud_summary, db["token"], clientkey="AO12345") == 404


def test_link_cannot_surface_a_device_moved_to_another_tenant(db):
    store.devices.docs[0]["client"] = "BEKO"
    assert _status(cloud.cloud_summary, db["token"], clientkey="AO12345") == 404


def _link():
    return store.cloud_links.docs[0]


def test_detail_requires_a_valid_grant(db):
    now = int(time.time())
    assert _status(cloud.cloud_detail, db["token"], clientkey="AO12345", x_cloud_grant=None) == 401
    grant, _ = cloud_links.issue_grant(_link(), now)
    tampered = grant[:-1] + ("0" if grant[-1] != "0" else "1")
    assert _status(cloud.cloud_detail, db["token"], clientkey="AO12345", x_cloud_grant=tampered) == 401
    expired, _ = cloud_links.issue_grant(_link(), now - cloud_links.GRANT_TTL_SECONDS - 5)
    assert _status(cloud.cloud_detail, db["token"], clientkey="AO12345", x_cloud_grant=expired) == 401
    # A grant for one link does not open another.
    other = cloud_links.create_link("device", db["device_id"], "AO", "AO12345", "paul@example.com", None)
    assert _status(cloud.cloud_detail, other, clientkey="AO12345", x_cloud_grant=grant) == 401

    out = cloud.cloud_detail(db["token"], clientkey="AO12345", x_cloud_grant=grant)
    item = out["items"][0]
    assert out["customerName"] == "Paul Smith"
    assert item["serial"].endswith("1203") and "SN-88" not in item["serial"]
    assert item["imei"].endswith("3809")
    assert item["purchasePrice"] == 449.99
    assert "registrationPhone" not in repr(out) and "+4477" not in repr(out)


def test_correct_code_issues_a_grant_once(db):
    code = cloud_links.issue_code(_link())
    out = cloud.cloud_otp_verify(db["token"], cloud.OtpVerifyBody(code=code), clientkey="AO12345")
    assert cloud_links.grant_valid(_link(), out["grant"], int(time.time()))
    # The same code cannot be replayed.
    assert _status(cloud.cloud_otp_verify, db["token"], cloud.OtpVerifyBody(code=code), clientkey="AO12345") == 400


def test_wrong_codes_lock_out_after_five_attempts(db):
    code = cloud_links.issue_code(_link())
    wrong = "000000" if code != "000000" else "111111"
    statuses = [_status(cloud.cloud_otp_verify, db["token"], cloud.OtpVerifyBody(code=wrong), clientkey="AO12345")
                for _ in range(5)]
    assert statuses == [400, 400, 400, 400, 429]
    # Even the right code is refused once locked.
    assert _status(cloud.cloud_otp_verify, db["token"], cloud.OtpVerifyBody(code=code), clientkey="AO12345") == 429


def test_expired_code_is_refused(db):
    code = cloud_links.issue_code(_link())
    store.cloud_otp.docs[-1]["expires_at"] = store.now() - timedelta(seconds=1)
    assert _status(cloud.cloud_otp_verify, db["token"], cloud.OtpVerifyBody(code=code), clientkey="AO12345") == 400


def test_new_code_supersedes_the_old_one(db):
    old = cloud_links.issue_code(_link())
    new = cloud_links.issue_code(_link())
    if old != new:
        assert _status(cloud.cloud_otp_verify, db["token"], cloud.OtpVerifyBody(code=old), clientkey="AO12345") == 400
    cloud.cloud_otp_verify(db["token"], cloud.OtpVerifyBody(code=new), clientkey="AO12345")


def test_sixth_code_request_in_an_hour_is_rate_limited(db):
    for _ in range(cloud_links.OTP_MAX_PER_HOUR):
        cloud_links.issue_code(_link())
    with pytest.raises(cloud_links.OtpError) as err:
        cloud_links.issue_code(_link())
    assert err.value.status == 429


def test_otp_request_for_another_tenant_sends_nothing(db, monkeypatch):
    sent = []

    async def fake_send(link, code):
        sent.append(code)
        return {"status": "sent"}

    monkeypatch.setattr(cloud, "send_otp_email", fake_send)
    import asyncio

    with pytest.raises(HTTPException) as err:
        asyncio.run(cloud.cloud_otp_request(db["token"], clientkey="BEKO1"))
    assert err.value.status_code == 404 and sent == []

    out = asyncio.run(cloud.cloud_otp_request(db["token"], clientkey="AO12345"))
    assert out == {"sent": True, "maskedEmail": "p***@e***.com"} and len(sent) == 1
