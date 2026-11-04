"""Regression tests for the 2026-11-03 security review findings.

- OTP: the code never reaches the requester, verifies once, expires server-side, and
  replicas share state (Mongo-backed, faked here).
- /customer/by-id: a ClientKey-pinned caller cannot read another tenant's customer.
- /dseo/webhook: postbacks must carry a signature bound to the MasterSKU they target.
"""
import asyncio
import json
import os

os.environ.setdefault("OTP_COOKIE_SECRET", "test-otp-secret-0123456789abcdef")

import pytest
from bson import ObjectId
from fastapi import HTTPException

from routers import otp
from routers.customer import get_by_id
from routers.enrich import dseo_webhook
from utils import dseo_auth


# --- a minimal in-memory stand-in for the pymongo calls otp.py makes -----------------------
def _matches(doc, flt):
    for key, cond in flt.items():
        value = doc.get(key)
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            if "$lt" in cond and not (value is not None and value < cond["$lt"]):
                return False
        elif value != cond:
            return False
    return True


class _Result:
    def __init__(self, n):
        self.modified_count = n


class FakeCollection:
    def __init__(self):
        self.docs = {}

    def create_index(self, *a, **k):
        return "ok"

    def find_one(self, flt, projection=None):
        for doc in self.docs.values():
            if _matches(doc, flt):
                return dict(doc)
        return None

    def replace_one(self, flt, doc, upsert=False):
        self.docs[doc["_id"]] = dict(doc)

    def update_one(self, flt, update):
        for doc in self.docs.values():
            if _matches(doc, flt):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                for k, v in update.get("$inc", {}).items():
                    doc[k] = doc.get(k, 0) + v
                return _Result(1)
        return _Result(0)


@pytest.fixture
def otp_env(monkeypatch):
    coll = FakeCollection()
    sent = []
    monkeypatch.setattr(otp, "otp_collection", coll)
    monkeypatch.setattr(otp, "_send_sms", lambda number, msg: sent.append((number, msg)))
    clock = {"ms": 1_800_000_000_000}
    monkeypatch.setattr(otp, "_now_ms", lambda: clock["ms"])
    return coll, sent, clock


PHONE = "+447700900123"


def _code_from(sent):
    return sent[-1][1].rsplit(" ", 1)[-1]


def test_request_does_not_disclose_the_code(otp_env):
    coll, sent, _ = otp_env
    body = otp.request_otp(otp.OtpRequestIn(phone=PHONE))
    code = _code_from(sent)
    assert code not in json.dumps(body)
    stored = json.dumps(list(coll.docs.values()), default=str)
    assert code not in stored, "only a hash of the code may be stored"


def test_code_verifies_once(otp_env):
    _, sent, _ = otp_env
    otp.request_otp(otp.OtpRequestIn(phone=PHONE))
    code = _code_from(sent)
    resp = otp.Response()
    assert otp.verify_otp(otp.OtpVerifyIn(phone=PHONE, code=code), resp) == {"success": True}
    with pytest.raises(HTTPException) as exc:
        otp.verify_otp(otp.OtpVerifyIn(phone=PHONE, code=code), otp.Response())
    assert exc.value.detail == "not_found"


def test_expired_code_is_rejected_server_side(otp_env):
    _, sent, clock = otp_env
    otp.request_otp(otp.OtpRequestIn(phone=PHONE))
    code = _code_from(sent)
    clock["ms"] += (otp.OTP_TTL_SECONDS + 1) * 1000
    with pytest.raises(HTTPException) as exc:
        otp.verify_otp(otp.OtpVerifyIn(phone=PHONE, code=code), otp.Response())
    assert exc.value.detail == "expired"


def test_attempts_are_capped(otp_env):
    _, sent, _ = otp_env
    otp.request_otp(otp.OtpRequestIn(phone=PHONE))
    code = _code_from(sent)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(otp.MAX_ATTEMPTS):
        with pytest.raises(HTTPException):
            otp.verify_otp(otp.OtpVerifyIn(phone=PHONE, code=wrong), otp.Response())
    with pytest.raises(HTTPException) as exc:
        otp.verify_otp(otp.OtpVerifyIn(phone=PHONE, code=code), otp.Response())
    assert exc.value.detail == "too_many_attempts"


def test_legacy_fallback_cookie_is_not_accepted(otp_env):
    """A code with no live server-side challenge fails, whatever cookies the client sends."""
    with pytest.raises(HTTPException) as exc:
        otp.verify_otp(otp.OtpVerifyIn(phone=PHONE, code="123456"), otp.Response())
    assert exc.value.detail == "not_found"


def test_send_rate_is_capped_per_number(otp_env):
    _, sent, clock = otp_env
    for _ in range(otp.MAX_SENDS_PER_WINDOW):
        otp.request_otp(otp.OtpRequestIn(phone=PHONE))
        clock["ms"] += (otp.RESEND_COOLDOWN_SECONDS + 1) * 1000
    with pytest.raises(HTTPException) as exc:
        otp.request_otp(otp.OtpRequestIn(phone=PHONE))
    assert exc.value.status_code == 429
    assert len(sent) == otp.MAX_SENDS_PER_WINDOW


def test_resend_inside_cooldown_sends_nothing(otp_env):
    _, sent, _ = otp_env
    otp.request_otp(otp.OtpRequestIn(phone=PHONE))
    assert otp.request_otp(otp.OtpRequestIn(phone=PHONE))["reused"] is True
    assert len(sent) == 1


# --- /customer/by-id tenant scoping ------------------------------------------------------
class _Coll:
    def __init__(self, doc=None):
        self.doc = doc

    def find_one(self, *a, **k):
        return dict(self.doc) if self.doc else None


def _customer(ours, theirs):
    return {
        "_id": ObjectId(),
        "telephone": PHONE,
        "devices": [{"deviceId": ours, "status": "contract"}, {"deviceId": theirs, "status": "registered"}],
        "marketingPreferences": [{"clientKey": "AO12345"}, {"clientKey": "BEKO1"}],
        "transaction_log": [{"x": 1}],
    }


def test_pinned_caller_cannot_read_another_tenants_customer(monkeypatch):
    doc = _customer(str(ObjectId()), str(ObjectId()))
    doc["marketingPreferences"] = [{"clientKey": "BEKO1"}]
    monkeypatch.setattr(get_by_id, "customer_collection", _Coll(doc))
    monkeypatch.setattr(get_by_id, "orders_collection", _Coll(None))
    monkeypatch.setattr(get_by_id, "client_id_for_key", lambda key: "AO")
    monkeypatch.setattr(get_by_id, "device_ids_owned_by", lambda cid, ids: set())

    with pytest.raises(HTTPException) as exc:
        get_by_id.get_customer_by_id(customer_id=str(doc["_id"]), scope="AO12345")
    assert exc.value.status_code == 404


def test_pinned_caller_sees_only_its_own_slice(monkeypatch):
    ours, theirs = str(ObjectId()), str(ObjectId())
    doc = _customer(ours, theirs)
    monkeypatch.setattr(get_by_id, "customer_collection", _Coll(doc))
    monkeypatch.setattr(get_by_id, "orders_collection", _Coll(None))
    monkeypatch.setattr(get_by_id, "client_id_for_key", lambda key: "AO")
    monkeypatch.setattr(get_by_id, "device_ids_owned_by", lambda cid, ids: {ours})

    data = get_by_id.get_customer_by_id(customer_id=str(doc["_id"]), scope="AO12345")["data"]
    assert [d["deviceId"] for d in data["devices"]] == [ours]
    assert data["marketingPreferences"] == [{"clientKey": "AO12345"}]
    assert "transaction_log" not in data


def test_unknown_pinned_key_fails_closed(monkeypatch):
    doc = _customer(str(ObjectId()), str(ObjectId()))
    monkeypatch.setattr(get_by_id, "customer_collection", _Coll(doc))
    monkeypatch.setattr(get_by_id, "client_id_for_key", lambda key: None)
    with pytest.raises(HTTPException) as exc:
        get_by_id.get_customer_by_id(customer_id=str(doc["_id"]), scope="NOPE")
    assert exc.value.status_code == 404


def test_unpinned_caller_still_gets_the_full_record(monkeypatch):
    ours, theirs = str(ObjectId()), str(ObjectId())
    doc = _customer(ours, theirs)
    monkeypatch.setattr(get_by_id, "customer_collection", _Coll(doc))
    data = get_by_id.get_customer_by_id(customer_id=str(doc["_id"]), scope=None)["data"]
    assert len(data["devices"]) == 2


# --- /dseo/webhook postback authentication ------------------------------------------------
class _FakeRequest:
    def __init__(self, query, body):
        self.query_params = query
        self._body = json.dumps(body).encode()
        self.headers = {}

    async def body(self):
        return self._body


def _call_webhook(monkeypatch, query, body):
    writes = []
    class _Inserted:
        inserted_id = "x"
    monkeypatch.setattr(dseo_webhook.dseo_results_collection, "insert_one", lambda rec: writes.append(rec) or _Inserted())
    # "skipped" so no cache re-warm is scheduled; only whether the task was dispatched matters.
    monkeypatch.setattr(dseo_webhook, "_process_task", lambda task: writes.append(task) or {"status": "skipped"})
    monkeypatch.setattr(dseo_webhook, "_process_product_info_task", lambda task: writes.append(task) or {"status": "skipped"})

    async def _body(request):
        return body
    monkeypatch.setattr(dseo_webhook, "_parse_body", _body)

    from fastapi import BackgroundTasks
    coro = dseo_webhook.dseo_webhook(_FakeRequest(query, body), BackgroundTasks())
    return asyncio.run(coro), writes


@pytest.fixture
def dseo_secret(monkeypatch):
    monkeypatch.setenv("DSEO_WEBHOOK_SECRET", "test-dseo-secret")


def _tasks(*tags):
    return {"tasks": [{"data": {"function": "products", "tag": t}} for t in tags]}


def test_unsigned_postback_is_rejected_and_nothing_written(monkeypatch, dseo_secret):
    with pytest.raises(HTTPException) as exc:
        _call_webhook(monkeypatch, {"id": "t1"}, _tasks(str(ObjectId())))
    assert exc.value.status_code == 401


def test_signature_for_one_sku_cannot_target_another(monkeypatch, dseo_secret):
    signed_for = str(ObjectId())
    with pytest.raises(HTTPException):
        _call_webhook(monkeypatch, {"id": "t1", "sig": dseo_auth.sign_tag(signed_for)}, _tasks(str(ObjectId())))


def test_mixed_tags_are_rejected(monkeypatch, dseo_secret):
    tag = str(ObjectId())
    with pytest.raises(HTTPException):
        _call_webhook(monkeypatch, {"id": "t1", "sig": dseo_auth.sign_tag(tag)}, _tasks(tag, str(ObjectId())))


def test_correctly_signed_postback_is_processed(monkeypatch, dseo_secret):
    tag = str(ObjectId())
    _, writes = _call_webhook(monkeypatch, {"id": "t1", "sig": dseo_auth.sign_tag(tag)}, _tasks(tag))
    assert len(writes) == 2  # audit record + processed task


def test_missing_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("DSEO_WEBHOOK_SECRET", raising=False)
    assert dseo_auth.verify_tag("abc", "anything") is False
    with pytest.raises(ValueError):
        dseo_auth.postback_url("https://api.example.com", "abc")


def test_postback_url_carries_signature(dseo_secret):
    url = dseo_auth.postback_url("https://api.example.com", "abc")
    assert url.startswith("https://api.example.com/dseo/webhook?id=$id&sig=")
    assert url.endswith(dseo_auth.sign_tag("abc"))
