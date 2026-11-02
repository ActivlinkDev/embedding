"""Regional pricing: a postcode-matched adjustment added after the rating factors.

The rule decides whether it applies (`when.regionalPricing`), the rating decides how
much (`regionAdjustment`), and the basket refuses a quote whose repair is booked for a
different postcode. Every gap fails safe: a line that cannot be adjusted is not priced.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from fastapi import HTTPException

from routers import product_assignment as pa
from routers import rate_request as rr
from utils.postcode import find_region, normalise_postcode


# ---- postcodes ----

@pytest.mark.parametrize("raw,locale,expected", [
    ("iv11hy", "en_GB", "IV1 1HY"),
    ("  IV1   1HY ", "en_GB", "IV1 1HY"),
    ("ec1a1bb", "en_GB", "EC1A 1BB"),
    ("hs12xq", "en_GB", "HS1 2XQ"),
    ("28013", "es_ES", "28013"),
    ("75 004", "fr_FR", "75004"),
    ("00184", "it_IT", "00184"),
    ("10178", "de_DE", "10178"),
    ("1000", "fr_BE", "1000"),
    ("1000", "nl_BE", "1000"),
    ("1012ab", "nl_NL", "1012 AB"),
])
def test_postcodes_normalise_to_one_spelling(raw, locale, expected):
    assert normalise_postcode(raw, locale) == expected


@pytest.mark.parametrize("raw,locale", [
    ("", "en_GB"), (None, "en_GB"), (12345, "es_ES"),
    ("NOT A POSTCODE", "en_GB"),
    ("2801", "es_ES"),        # four digits is Belgian, not Spanish
    ("28013", "fr_BE"),       # five digits is not Belgian
    ("IV1 1HY", "fr_FR"),     # a UK postcode on a French journey
    ("12345", "xx_ZZ"),       # a country we cannot check is not guessed at
])
def test_anything_else_is_not_a_postcode(raw, locale):
    assert normalise_postcode(raw, locale) is None


REGIONS = [
    {"prefix": "B", "label": "Birmingham", "adjustment": 1},
    {"prefix": "IV", "label": "Highlands", "adjustment": 15},
    {"prefix": "IV1", "label": "Inverness centre", "adjustment": 5},
    {"prefix": "IV1 1", "label": "Inverness sector", "adjustment": 4},
    {"prefix": "*", "label": "Standard", "adjustment": 0},
]


@pytest.mark.parametrize("postcode,label", [
    ("IV1 1HY", "Inverness sector"),   # sector beats district beats area
    ("IV1 2AB", "Inverness centre"),
    ("IV12 3AB", "Highlands"),         # IV1 must not match IV12
    ("BT1 1AA", "Standard"),           # B (Birmingham) must not match BT (Belfast)
    ("B1 1AA", "Birmingham"),
    ("SW1A 2DX", "Standard"),
])
def test_uk_rows_match_on_postcode_structure_not_characters(postcode, label):
    assert find_region(REGIONS, postcode, "en_GB")["label"] == label


def test_numeric_countries_match_by_prefix():
    regions = [{"prefix": "75", "label": "Paris", "adjustment": 10},
               {"prefix": "750", "label": "Paris centre", "adjustment": 12}]
    assert find_region(regions, "75004", "fr_FR")["label"] == "Paris centre"
    assert find_region(regions, "76000", "fr_FR") is None


def test_no_match_and_no_default_is_none():
    assert find_region([{"prefix": "HS", "adjustment": 25}], "SW1A 2DX", "en_GB") is None


def test_malformed_rows_are_skipped():
    regions = [{"adjustment": 9}, "junk", {"prefix": "", "adjustment": 9},
               {"prefix": "HS", "adjustment": 25}]
    assert find_region(regions, "HS1 2XQ", "en_GB")["adjustment"] == 25


# ---- the rule switches it on ----

@pytest.mark.parametrize("value,expected", [
    (True, True), ("true", True), (None, False), (False, False), ("false", False),
    ("maybe", False), (1.5, False),
])
def test_only_a_real_true_turns_regional_pricing_on(value, expected):
    rule = {"ruleId": "R", "when": {} if value is None else {"regionalPricing": value}}
    assert pa.rule_regional_pricing(rule) is expected


def test_regional_pricing_is_not_a_matching_condition():
    """A request with no postcode must still match the rule, not fall past it."""
    payload = pa.ProductAssignmentRequest(
        client="AO", source="POS", category="Washer Dryer", price=449.99, locale="en_GB",
        purchase_date="2025-05-01", gtee=12, currency="GBP", device_working=False)
    rule = {"when": {"locale": ["en_GB"], "currency": ["GBP"], "deviceWorking": False,
                     "regionalPricing": True}}
    assert pa.when_failure_reasons(rule, payload, 6) == []


# ---- the rating ----

TAXONOMY = {"Washer Dryer": {"category": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances"}}


@pytest.fixture
def rating(monkeypatch):
    from conftest import seed_taxonomy
    seed_taxonomy(monkeypatch, TAXONOMY)
    doc = {
        "_id": ObjectId(), "currency": "GBP", "products": ["REPAIR"], "status": "active",
        "baseFee": 50,
        "localeFactor": [{"locale": "en_GB", "factor": 1}],
        "pocFactor": {"12": 1},
        "categoryFactor": [{"level": "category", "value": "Washer Dryer", "factor": 1}],
        "ageFactor": [{"minMonths": 0, "maxMonths": 120, "factor": 1}],
        "priceFactor": [{"min": 0, "max": 5000, "factor": 1}],
        "multiFactor": [{"min": 1, "max": 10, "factor": 1}],
        "regionAdjustment": {
            "minimumPrice": 40,
            "regions": [
                {"prefix": "HS", "label": "Outer Hebrides", "adjustment": 25},
                {"prefix": "EC", "label": "City", "adjustment": -5},
                {"prefix": "*", "label": "Standard", "adjustment": 0},
            ],
        },
    }
    ratings = MagicMock()
    ratings.find.side_effect = lambda *a, **k: [doc]
    monkeypatch.setattr(rr, "ratings", ratings)
    monkeypatch.setattr(rr, "error_log_collection", MagicMock())
    monkeypatch.setattr(rr, "_ensure_indexes", lambda: None)
    return doc


def line(**over):
    base = dict(product_id="REPAIR", currency="GBP", locale="en_GB", poc=12,
                category="Washer Dryer", age=6, price=449.99, multi_count=1,
                client="AO", source="POS", mode="payment")
    base.update(over)
    return rr.RateRequest(**base)


def priced(req):
    grouped, _ = rr.price_and_group([req], log_errors=False)
    return grouped[0]["options"][0]


def test_the_adjustment_is_added_after_the_factors_and_before_rounding(rating):
    option = priced(line(regional_pricing=True, postcode="HS1 2XQ"))
    assert option["status"] == "ok"
    assert option["rate"] == 75.0                 # 50 × factors, + 25
    assert option["rounded_price"] == 75.49       # then onto a price point
    assert option["rounded_price_pence"] == 7549
    region = option["factors"]["region_adjustment"]
    assert region == {"postcode": "HS1 2XQ", "prefix": "HS", "label": "Outer Hebrides",
                      "adjustment": 25.0, "rate_before_adjustment": 50.0}


def test_a_negative_adjustment_deducts(rating):
    assert priced(line(regional_pricing=True, postcode="EC1A 1BB"))["rounded_price"] == 45.49


def test_an_unlisted_postcode_takes_the_default_row(rating):
    option = priced(line(regional_pricing=True, postcode="SW1A 2DX"))
    assert option["rounded_price"] == 50.49
    assert option["factors"]["region_adjustment"]["prefix"] == "*"


def test_a_deduction_below_the_minimum_fails_the_line(rating):
    rating["regionAdjustment"]["minimumPrice"] = 49.99
    option = priced(line(regional_pricing=True, postcode="EC1A 1BB"))
    assert option["status"] == "error"
    assert "rounded_price" not in option


def test_a_deduction_to_nothing_fails_even_with_no_minimum(rating):
    rating["regionAdjustment"].pop("minimumPrice")
    rating["regionAdjustment"]["regions"][1]["adjustment"] = -60
    assert priced(line(regional_pricing=True, postcode="EC1A 1BB"))["status"] == "error"


@pytest.mark.parametrize("change", ["no_table", "no_default", "no_postcode", "text_amount"])
def test_a_line_that_cannot_be_adjusted_is_not_priced_without_it(rating, change):
    postcode = "SW1A 2DX"
    if change == "no_table":
        rating.pop("regionAdjustment")
    elif change == "no_default":
        rating["regionAdjustment"]["regions"].pop()
    elif change == "no_postcode":
        postcode = None
    elif change == "text_amount":
        rating["regionAdjustment"]["regions"][2]["adjustment"] = "0"
    assert priced(line(regional_pricing=True, postcode=postcode))["status"] == "error"


def test_without_the_rule_flag_the_table_is_ignored(rating):
    option = priced(line(postcode="HS1 2XQ"))
    assert option["rounded_price"] == 50.49
    assert "region_adjustment" not in option["factors"]


def test_the_new_fields_are_not_counted_as_missing():
    assert line().missing_fields() == []


# ---- embedded_quote ----

@pytest.fixture
def quote_route(monkeypatch):
    from routers import embedded_quote as eq
    stored = {}

    def assign(device_id, device_working=None):
        return {"Products": [{
            "product_id": "REPAIR", "currency": "GBP", "locale": "en_GB", "poc": 12,
            "category": "Washer Dryer", "age": 6, "price": 449.99, "multi_count": 1,
            "client": "AO", "source": "POS", "mode": "payment", "regional_pricing": True}]}

    def price(requests):
        stored["requests"] = requests
        return [{"product_id": "REPAIR", "options": []}], None

    def store(grouped, device_id=None, client_key=None, extra=None):
        stored["extra"] = extra
        return "QUOTE"

    monkeypatch.setattr(eq, "assign_product_for_device", assign)
    monkeypatch.setattr(eq, "price_and_group", price)
    monkeypatch.setattr(eq, "store_quote", store)
    return eq, stored


def run_quote(eq, **body):
    return asyncio.run(eq.embedded_quote(
        eq.EmbeddedQuoteRequest(device_id=str(ObjectId()), clientKey="AOPON12345", **body), None))


@pytest.mark.parametrize("postcode,code", [(None, "postcode_required"), ("", "postcode_required"),
                                           ("NOPE", "postcode_invalid")])
def test_a_regional_quote_without_a_usable_postcode_prices_nothing(quote_route, postcode, code):
    eq, stored = quote_route
    with pytest.raises(HTTPException) as error:
        run_quote(eq, postcode=postcode)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == code
    assert stored == {}


def test_a_regional_quote_is_priced_and_stored_with_its_postcode(quote_route):
    eq, stored = quote_route
    assert run_quote(eq, postcode="hs1 2xq")["quote_id"] == "QUOTE"
    assert stored["requests"][0].regional_pricing is True
    assert stored["requests"][0].postcode == "HS1 2XQ"
    assert stored["extra"] == {"postcode": "HS1 2XQ", "regionalPricing": True}


# ---- the widget cache ----

@pytest.fixture
def widget(monkeypatch):
    from routers import widget_quote as w
    cache = MagicMock()
    cache.find_one.return_value = None
    monkeypatch.setattr(w, "widget_cache_collection", cache)
    request = SimpleNamespace(price=449.99, gtee=12, currency="GBP", working=False, locale="en_GB",
                              category="Washer Dryer", client="AO", source="POS")
    monkeypatch.setattr(w, "resolve_widget_inputs", lambda payload: (request, 6, None, None, None))
    monkeypatch.setattr(w, "assign_products", lambda req: {
        "products": [{"productId": "REPAIR", "mode": "payment", "terms": [12]}],
        "regional_pricing": True})
    monkeypatch.setattr(w, "price_and_group", lambda requests: ([{"product_id": "REPAIR"}], (0, 1000)))
    return w, cache


def widget_payload(w, postcode):
    return w.WidgetPriceRequest(clientKey="AOPON12345", customSkuId=str(ObjectId()), price=449.99,
                                locale="en_GB", postcode=postcode)


def test_regional_widget_options_are_cached_under_their_postcode(widget):
    w, cache = widget
    _grouped, _currency, postcode = w._priced_options(widget_payload(w, "hs12xq"))
    assert postcode == "HS1 2XQ"
    reads = [c.args[0]["postcode"] for c in cache.find_one.call_args_list]
    assert reads == [None, "HS1 2XQ"]
    assert cache.update_one.call_args.args[0]["postcode"] == "HS1 2XQ"


def test_a_regional_widget_price_needs_a_postcode(widget):
    w, cache = widget
    with pytest.raises(HTTPException) as error:
        w._priced_options(widget_payload(w, None))
    assert error.value.status_code == 422
    cache.update_one.assert_not_called()


# ---- the basket ----

@pytest.fixture
def basket(monkeypatch):
    from routers import basket as b
    requests = MagicMock()
    monkeypatch.setattr(b, "service_requests_collection", requests)
    return b, requests


def regional_quote(**over):
    return {"deviceId": "DEV1", "regionalPricing": True, "postcode": "HS1 2XQ", **over}


def test_a_repair_booked_for_the_quoted_postcode_is_accepted(basket):
    b, requests = basket
    requests.find_one.return_value = {"postcode": "HS1 2XQ"}
    b.assert_quote_region_matches_repair(regional_quote(), str(ObjectId()))
    # Looked up through the quote's own device, so another tenant's request cannot pass.
    assert requests.find_one.call_args.args[0]["deviceId"] == "DEV1"


@pytest.mark.parametrize("stored", [{"postcode": "EC1A 1BB"}, {"postcode": None}, {"_id": "SR"}])
def test_a_repair_booked_elsewhere_is_refused(basket, stored):
    b, requests = basket
    requests.find_one.return_value = stored
    with pytest.raises(HTTPException) as error:
        b.assert_quote_region_matches_repair(regional_quote(), str(ObjectId()))
    assert error.value.status_code == 409


def test_a_service_request_for_another_device_is_not_found(basket):
    b, requests = basket
    requests.find_one.return_value = None
    with pytest.raises(HTTPException) as error:
        b.assert_quote_region_matches_repair(regional_quote(), str(ObjectId()))
    assert error.value.status_code == 404


@pytest.mark.parametrize("quote,sr_id", [({"deviceId": "DEV1"}, "x"), (regional_quote(), None)])
def test_ordinary_quotes_and_cover_lines_are_not_checked(basket, quote, sr_id):
    b, requests = basket
    b.assert_quote_region_matches_repair(quote, sr_id)
    requests.find_one.assert_not_called()


# ---- the repair address ----

from utils.postcode import address_postcode


@pytest.mark.parametrize("raw,locale,expected", [
    ("iv11hy", "en_GB", "IV1 1HY"),
    ("nonsense", "en_GB", None),         # a known format is enforced
    (" d02 x285 ", "en_IE", "D02 X285"),  # an unknown one is tidied, never blocked
    ("<script>", "en_IE", None),
])
def test_an_address_postcode_is_never_blocked_by_an_unknown_format(raw, locale, expected):
    assert address_postcode(raw, locale) == expected


@pytest.fixture
def sr_db(monkeypatch):
    import routers.service_requests as package
    from routers.service_requests import service as svc
    requests, devices = MagicMock(), MagicMock()
    requests.insert_one.return_value.inserted_id = ObjectId()
    devices.find_one.return_value = {"_id": ObjectId(), "client": "AO", "identifiers": {"category": "Dishwasher"}}
    monkeypatch.setattr(package, "service_requests_collection", requests)
    monkeypatch.setattr(svc, "service_requests_collection", requests)
    monkeypatch.setattr(svc, "devices_collection", devices)
    monkeypatch.setattr(svc, "client_id_for_key", lambda key: "AO")
    monkeypatch.setattr(package, "next_reference", lambda: "SRV-1")
    monkeypatch.setattr(svc, "media_for", lambda *a, **k: [])
    return package, requests


def create(package, postcode):
    return package.create_service_request(package.CreateServiceRequest(
        clientkey="AOPON12345", deviceId=str(ObjectId()), locale="en_GB",
        fault={"freeText": "Drum will not spin"}, postcode=postcode), None)


def test_the_repair_postcode_is_stored_normalised(sr_db):
    package, requests = sr_db
    create(package, "hs1 2xq")
    assert requests.insert_one.call_args.args[0]["postcode"] == "HS1 2XQ"


def test_an_invalid_repair_postcode_writes_nothing(sr_db):
    package, requests = sr_db
    with pytest.raises(HTTPException) as error:
        create(package, "nonsense")
    assert error.value.status_code == 422
    requests.insert_one.assert_not_called()


def test_an_appointment_for_a_different_postcode_is_refused(sr_db):
    package, requests = sr_db
    requests.find_one.return_value = {"_id": ObjectId(), "client": "AO", "status": "REPORTED",
                                      "locale": "en_GB", "postcode": "HS1 2XQ"}
    with pytest.raises(HTTPException) as error:
        package.set_appointment(package.SetAppointmentRequest(
            clientkey="AOPON12345", serviceRequestId=str(ObjectId()), deviceId=str(ObjectId()),
            date="2099-01-01", contact={"postcode": "EC1A 1BB"}), None)
    assert error.value.status_code == 409
    requests.find_one_and_update.assert_not_called()
