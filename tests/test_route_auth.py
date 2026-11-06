"""Routes that once took no bearer token must now refuse a request without a valid one.

Each request is otherwise well formed, so a pass shows the auth layer answering before the
handler runs: no Stripe call, no customer write, no device read.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import generate_payment_link, generate_payment_links_from_quote, sync_stripe_prices
from routers.customer import create_customer, pair_customer
from routers.devices import get_device_by_id

app = FastAPI()
for module in (
    sync_stripe_prices,
    generate_payment_link,
    generate_payment_links_from_quote,
    create_customer,
    pair_customer,
    get_device_by_id,
):
    app.include_router(module.router)

client = TestClient(app)

ROUTES = [
    ("POST", "/sync_stripe_prices", None),
    ("POST", "/generate_checkout_session", {}),
    ("POST", "/generate_payment_link", {"quote_id": "q", "optionref": "o"}),
    ("POST", "/get-or-create-customer", {"name": "A", "telephone": "+447700900000", "email": "a@b.co"}),
    ("POST", "/pair-customer", {"customerId": "c", "basketId": "b"}),
    ("GET", "/devices/by-id?device_id=65f000000000000000000000", None),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_refused_without_a_token(method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code in (401, 403), response.text
    assert response.json()["detail"] == "Not authenticated"


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_refused_with_a_bad_token(method, path, body):
    response = client.request(method, path, json=body, headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Invalid or missing token"


def test_schema_marks_them_as_secured():
    schema = app.openapi()
    for method, path, _ in ROUTES:
        operation = schema["paths"][path.split("?")[0]][method.lower()]
        assert operation.get("security"), path
        assert "401" in operation["responses"], path
