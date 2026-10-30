"""Brevo confirmation emails: tenant resolution, skips, dedupe and failure handling."""
import asyncio
import json

import httpx
import pytest
from bson import ObjectId

from routers.comms import brevo_client, cloud_links, confirmation, email_config, store
from tests.fake_mongo import FakeCollection

CLIENT_ID = "AO"
CLIENT_KEY = "AO12345"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("CLOUD_ACCESS_SECRET", "s" * 40)
    monkeypatch.setenv("BREVO_TEMPLATE_PLAN", "")
    monkeypatch.setattr(email_config, "STRAPI_BASE", "")
    customer_id = ObjectId()
    device_id = ObjectId()
    other_device = ObjectId()
    cols = {
        "client_keys": FakeCollection([
            {"ClientKey": CLIENT_KEY, "Client_ID": CLIENT_ID, "Client_Name": "AO.com", "Source": "web",
             "redirect_base_url": "https://ao.registermyproduct.io",
             "Styles": {"Accent": "#112233", "Logo_URL": "https://cdn.example.com/ao.png"}},
            {"ClientKey": "BEKO1", "Client_ID": "BEKO", "Client_Name": "Beko", "Source": "web"},
        ]),
        "email_config": FakeCollection([{"client_key": CLIENT_KEY, "settings": {
            "enabled": {"registration": True, "plan": True}, "templateIds": {"plan": 42}}}], unique=["client_key"]),
        "customers": FakeCollection([{"_id": customer_id, "name": "Paul Smith", "email": "paul@example.com"}]),
        "devices": FakeCollection([
            {"_id": device_id, "client": CLIENT_ID, "source": "web", "locale": "en_GB",
             "identifiers": {"title": "Washing machine", "make": "Bosch", "model": "WGG", "category": "Laundry"},
             "uniqueParameters": {"serial": "SN-8841203", "imei": "356938035643809"},
             "registrationParameters": {"customerId": str(customer_id), "price": 449.99}},
            {"_id": other_device, "client": "BEKO", "source": "web", "locale": "en_GB",
             "identifiers": {"title": "Fridge"},
             "registrationParameters": {"customerId": str(customer_id)}},
        ]),
        "contracts": FakeCollection(),
        "baskets": FakeCollection(),
        "registrations": FakeCollection(),
        "email_log": FakeCollection(unique=["dedupe_key"]),
        "cloud_links": FakeCollection(unique=["token_hash"]),
        "cloud_otp": FakeCollection(),
    }
    for name, col in cols.items():
        monkeypatch.setattr(store, name, col)
    cols.update(customer_id=str(customer_id), device_id=str(device_id), other_device=str(other_device))
    return cols


@pytest.fixture
def brevo(monkeypatch):
    """Capture Brevo calls; `responses` is a list of status codes or exceptions to return."""
    calls, responses = [], []

    def handler(request):
        calls.append(json.loads(request.content))
        outcome = responses.pop(0) if responses else 201
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={"messageId": "<m1@brevo>"} if outcome < 400 else {"message": "bad"})

    transport = httpx.MockTransport(handler)
    real = brevo_client.BrevoEmailClient

    def factory(**kwargs):
        return real(api_key="test-key", transport=transport, backoff_base=0, **kwargs)

    monkeypatch.setattr(confirmation, "BrevoEmailClient", factory)
    return calls, responses


def run(coro):
    return asyncio.run(coro)


def test_registration_email_is_sent_once_with_cloud_links_and_no_identifiers(db, brevo):
    calls, _ = brevo

    first = run(confirmation.send_registration_confirmation(db["customer_id"], [db["device_id"]]))
    again = run(confirmation.send_registration_confirmation(db["customer_id"], [db["device_id"]]))

    assert first == {"status": "sent"}
    assert again == {"status": "duplicate"}
    assert len(calls) == 1
    payload = calls[0]
    assert payload["to"] == [{"email": "paul@example.com", "name": "Paul Smith"}]
    assert "htmlContent" in payload  # no template configured for registration -> built-in layout
    params = payload["params"]
    assert params["clientName"] == "AO.com"
    assert params["primaryColour"] == "#112233"
    assert params["logoUrl"] == "https://cdn.example.com/ao.png"
    assert params["copy"]["greeting"] == "Hi Paul,"
    item = params["items"][0]
    assert item["cloudUrl"].startswith("https://ao.registermyproduct.io/cloud/")
    body = json.dumps(payload)
    for secret in ("SN-8841203", "356938035643809", "449.99"):
        assert secret not in body
    # The token in the email is not stored; only its hash is.
    token = item["cloudUrl"].split("/cloud/")[1].split("?")[0]
    link = store.cloud_links.docs[0]
    assert link["token_hash"] == store.token_hash(token) and token not in json.dumps(link, default=str)
    assert link["client_id"] == CLIENT_ID
    log = store.email_log.docs[0]
    assert log["status"] == "sent" and log["to_masked"] == "p***@e***.com"
    assert "paul@example.com" not in json.dumps(log, default=str)


def test_registration_email_never_mixes_another_tenants_device(db, brevo):
    calls, _ = brevo

    run(confirmation.send_registration_confirmation(db["customer_id"], [db["device_id"], db["other_device"]]))

    titles = [i["title"] for i in calls[0]["params"]["items"]]
    assert titles == ["Washing machine"] or titles == ["Fridge"]
    assert len(titles) == 1


@pytest.mark.parametrize("email", ["user_12345@placeholder.local", "", None, "not-an-email"])
def test_no_deliverable_address_is_skipped_without_calling_brevo(db, brevo, email):
    calls, _ = brevo
    store.customers.docs[0]["email"] = email

    result = run(confirmation.send_registration_confirmation(db["customer_id"], [db["device_id"]]))

    assert result["status"] == "skipped"
    assert calls == []
    assert store.cloud_links.docs == []


def test_disabled_client_is_skipped(db, brevo):
    calls, _ = brevo
    store.email_config.docs[0]["settings"]["enabled"]["registration"] = False

    result = run(confirmation.send_registration_confirmation(db["customer_id"], [db["device_id"]]))

    assert result == {"status": "skipped", "reason": "disabled for client"}
    assert calls == []


def test_client_without_saved_settings_sends_nothing(db, brevo):
    calls, _ = brevo
    store.email_config.docs.clear()

    result = run(confirmation.send_registration_confirmation(db["customer_id"], [db["device_id"]]))

    assert result["status"] == "skipped"
    assert calls == []


def _contract(db, **extra):
    doc = {"_id": ObjectId(), "order_reference": "ORD-2026-000001", "reference": "ACT-2026-000001",
           "device_id": db["device_id"], "customer_id": db["customer_id"], "status": "ACTIVE",
           "quote_id": "q1", "basket_id": "", "price": 89.0, **extra}
    store.contracts.insert_one(doc)
    return doc


def test_plan_email_uses_client_template_and_replay_sends_nothing(db, brevo):
    calls, _ = brevo
    contract = _contract(db)

    first = run(confirmation.send_plan_confirmation([contract], db["customer_id"]))
    replay = run(confirmation.send_plan_confirmation([contract], db["customer_id"]))

    assert first == {"status": "sent"} and replay == {"status": "duplicate"}
    assert len(calls) == 1
    assert calls[0]["templateId"] == 42 and "htmlContent" not in calls[0]
    item = calls[0]["params"]["items"][0]
    assert item["reference"] == "ACT-2026-000001"
    assert "89" not in json.dumps(calls[0]["params"])


def test_failed_send_is_recorded_not_retried_and_can_be_reclaimed(db, brevo):
    calls, responses = brevo
    responses.extend([httpx.ReadTimeout("slow"), 201])
    contract = _contract(db)

    failed = run(confirmation.send_plan_confirmation([contract], db["customer_id"]))
    assert failed == {"status": "failed"}
    assert len(calls) == 1  # a timeout may have been accepted by Brevo: no automatic retry
    assert store.email_log.docs[0]["status"] == "failed"

    retried = run(confirmation.send_plan_confirmation([contract], db["customer_id"]))
    assert retried == {"status": "sent"}
    assert store.email_log.docs[0]["retries"] == 1


def test_brevo_rejection_does_not_raise(db, brevo):
    _, responses = brevo
    responses.append(400)

    result = run(confirmation.send_plan_confirmation([_contract(db)], db["customer_id"]))

    assert result == {"status": "failed"}


def test_rate_limit_is_the_only_retried_status(db, brevo):
    calls, responses = brevo
    responses.extend([429, 201])

    assert run(confirmation.send_plan_confirmation([_contract(db)], db["customer_id"])) == {"status": "sent"}
    assert len(calls) == 2


def test_missing_strapi_copy_falls_back_to_english(db, brevo, monkeypatch):
    calls, _ = brevo
    store.devices.docs[0]["locale"] = "fr_BE"

    async def partial(locale):
        return {"locale": locale, "Registration_Heading": "Merci"} if locale == "fr" else {}

    monkeypatch.setattr(email_config, "_fetch_strapi_copy", partial)

    run(confirmation.send_registration_confirmation(db["customer_id"], [db["device_id"]]))

    copy = calls[0]["params"]["copy"]
    assert copy["heading"] == "Merci"
    assert copy["cta"] == email_config.DEFAULT_COPY["Registration_Cta"]


def test_sender_off_the_verified_domains_is_refused_on_save(monkeypatch):
    monkeypatch.setenv("BREVO_ALLOWED_SENDER_DOMAINS", "ao.com")
    with pytest.raises(ValueError):
        email_config.EmailSettings.model_validate({"senderEmail": "promo@evil.example"})
    ok = email_config.EmailSettings.model_validate({"senderEmail": "hello@ao.com", "primaryColour": "#0B5FFF"})
    assert ok.senderEmail == "hello@ao.com"
    with pytest.raises(ValueError):
        email_config.EmailSettings.model_validate({"logoUrl": "http://insecure.example/logo.png"})


def test_fallback_html_escapes_customer_data():
    ctx_params = {"copy": {"heading": "<b>h</b>", "greeting": "Hi <script>", "intro": "i", "footer": "f",
                           "cta": "Go", "labels": {"reference": "R", "cover": "C", "startDate": "S", "endDate": "E"}},
                  "primaryColour": "red;background:url(x)", "buttonColour": "#123456", "logoUrl": "",
                  "clientName": "AO", "items": [{"title": "<img src=x>", "cloudUrl": "https://x/\"><script>"}]}
    out = email_config.render_fallback_html(ctx_params)
    assert "<script>" not in out and "<img src=x>" not in out
    assert "url(x)" not in out


def test_masking_helpers():
    assert store.mask_email("paul.smith@gmail.com") == "p***@g***.com"
    assert store.mask_identifier("356938035643809") == "••••••••3809"
    assert store.deliverable_email("User_1@Placeholder.Local") is None
