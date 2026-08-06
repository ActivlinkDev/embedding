from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from pymongo import MongoClient
from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from datetime import datetime
import os
import re
from typing import List, Optional

router = APIRouter(tags=["Payments"])

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
ratings = db["Rating"]
error_log_collection = db["Error_Log_RateRequest"]
stripe_payment_collection = db["Stripe_Price_ID"]
quotes_collection = db["Quotes"]
error_log_stripe_collection = db["Error_Log_Stripe"]

# --- Models ---

class RateRequest(BaseModel):
    """One product/term combination to price. **Every field is mandatory**, and each one has to
    match the rating configuration — a value with no configured factor fails that line.

    These are exactly the entries returned by `GET /assign_product_for_device/{device_id}`, so
    the usual flow is to pass those straight through rather than assembling them by hand.
    """

    product_id: str = Field(..., description="**Mandatory.** The cover product being priced.", examples=["ACME-EW-STD"])
    currency: str = Field(..., description="**Mandatory.** ISO 4217 currency, e.g. `GBP`.", examples=["GBP"])
    locale: str = Field(..., description="**Mandatory.** Locale; must have a configured `localeFactor`.", examples=["en_GB"])
    poc: int = Field(
        ...,
        description="**Mandatory.** Period of cover in months; must have a configured `pocFactor`.",
        examples=[24],
    )
    category: str = Field(..., description="**Mandatory.** Device category; must have a configured `categoryFactor`.", examples=["Dishwasher"])
    age: int = Field(
        ...,
        description="**Mandatory.** Device age in months; must have a configured `ageFactor`. `0` is valid here.",
        examples=[15],
    )
    price: float = Field(
        ...,
        description="**Mandatory.** Device price; must fall inside a configured `priceFactor` band.",
        examples=[449.99],
    )
    multi_count: int = Field(
        ...,
        description="**Mandatory.** Number of devices covered together; must have a configured `multiFactor`.",
        examples=[1],
    )
    client: str = Field(..., description="**Mandatory.** `Client_ID` whose rating configuration applies.", examples=["ACME-UK"])
    source: str = Field(..., description="**Mandatory.** Sales channel the rating is defined for.", examples=["web"])
    mode: str = Field(..., description="**Mandatory.** Rating mode, e.g. `live` or `test`.", examples=["live"])

    def missing_fields(self):
        missing = []
        for field in self.model_fields:
            val = getattr(self, field)
            if field == "age":
                if val in ("", None):  # Only blank/None age is missing, 0 is valid
                    missing.append(field)
            else:
                if val in ("", None) or (isinstance(val, (int, float)) and val == 0):
                    missing.append(field)
        return missing

class RateRequestBatch(BaseModel):
    """A batch of lines to price together and store as one quote."""

    deviceId: Optional[str] = Field(
        None,
        description="Device this quote is for. Stored on the quote so it can be found later.",
        examples=["6820f1c9a4b21d0f8c9e4471"],
    )
    clientKey: Optional[str] = Field(
        None,
        description="Tenant key recorded against the quote. Recommended — it is how quotes are attributed.",
        examples=["acme_uk_live"],
    )
    requests: List[RateRequest] = Field(..., description="**Mandatory.** One entry per product/term combination to price.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "deviceId": "6820f1c9a4b21d0f8c9e4471",
                "clientKey": "acme_uk_live",
                "requests": [
                    {
                        "product_id": "ACME-EW-STD",
                        "currency": "GBP",
                        "locale": "en_GB",
                        "poc": 24,
                        "category": "Dishwasher",
                        "age": 15,
                        "price": 449.99,
                        "multi_count": 1,
                        "client": "ACME-UK",
                        "source": "web",
                        "mode": "live",
                    },
                    {
                        "product_id": "ACME-EW-STD",
                        "currency": "GBP",
                        "locale": "en_GB",
                        "poc": 36,
                        "category": "Dishwasher",
                        "age": 15,
                        "price": 449.99,
                        "multi_count": 1,
                        "client": "ACME-UK",
                        "source": "web",
                        "mode": "live",
                    },
                ],
            }
        }
    }

# --- Utilities (Unchanged) ---

def normalize(s):
    return re.sub(r'\W+', '', (s or '')).strip().lower()

def find_price_factor(price_factor_list, price):
    for pf in price_factor_list:
        if pf["priceLow"] <= price <= pf["priceHigh"]:
            return pf["factor"]
    return None

def find_price_bracket(price_factor_list, price):
    """Return (priceLow, priceHigh) for the bracket containing `price`, or None."""
    for pf in price_factor_list:
        if pf["priceLow"] <= price <= pf["priceHigh"]:
            return (pf["priceLow"], pf["priceHigh"])
    return None

def round_price_49_99(value):
    cents = round(value % 1, 2)
    whole = int(value)
    if abs(cents - 0.49) < 0.001 or abs(cents - 0.99) < 0.001:
        return round(value, 2)
    if cents < 0.49:
        return round(whole + 0.49, 2)
    elif cents < 0.99:
        return round(whole + 0.99, 2)
    else:
        return round(whole + 1.49, 2)

def match_with_reasons(doc, payload):
    reasons = []

    if doc.get("currency") != payload.currency:
        reasons.append(f"currency '{payload.currency}' not matched")
    if payload.product_id not in doc.get("productID", []):
        reasons.append(f"product_id '{payload.product_id}' not in productID")
    if not any(normalize(lf.get("locale", "")) == normalize(payload.locale) for lf in doc.get("localeFactor", [])):
        reasons.append(f"locale '{payload.locale}' not matched in localeFactor")
    if str(payload.poc) not in doc.get("pocFactor", {}):
        reasons.append(f"poc '{payload.poc}' not found in pocFactor")
    if not any(normalize(cf.get("device", "")) == normalize(payload.category) for cf in doc.get("categoryFactor", [])):
        reasons.append(f"category '{payload.category}' not matched in categoryFactor")
    if str(payload.age) not in doc.get("ageFactor", {}):
        reasons.append(f"age '{payload.age}' not found in ageFactor")
    price_match = False
    for pf in doc.get("priceFactor", []):
        if pf["priceLow"] <= payload.price <= pf["priceHigh"]:
            price_match = True
            break
    if not price_match:
        reasons.append(f"price '{payload.price}' not in any priceFactor range")
    if str(payload.multi_count) not in doc.get("multiFactor", {}):
        reasons.append(f"multi_count '{payload.multi_count}' not found in multiFactor")

    return len(reasons) == 0, reasons

def extract_lang_from_locale(locale: str) -> str:
    """
    Extracts the 2-letter language code from a locale string like 'en_US' or 'es-MX' or 'fr'
    """
    if not locale:
        return ""
    return re.split(r'[_-]', locale)[0].lower()

# --- Grouping Utility ---

def group_responses(responses):
    groups = {}
    group_keys = [
        "product_id", "client", "currency", "locale", "category", "age",
        "price", "multi_count", "source", "lang"
    ]
    for resp in responses:
        group_key = tuple(resp.get(k) for k in group_keys)
        if group_key not in groups:
            group_obj = {k: resp.get(k) for k in group_keys}
            group_obj["options"] = []
            groups[group_key] = group_obj
        option = {
            "status": resp.get("status"),
            "poc": resp.get("poc"),
            "mode": resp.get("mode"),
            "rate": resp.get("rate"),
            "rounded_price": resp.get("rounded_price"),
            "rounded_price_pence": resp.get("rounded_price_pence"),
            "factors": resp.get("factors"),
            "error": resp.get("error") if "error" in resp else None
        }
        # Remove None fields
        option = {k: v for k, v in option.items() if v is not None}
        groups[group_key]["options"].append(option)
    return list(groups.values())

# --- Core pricing (no persistence) ---

def price_and_group(requests: List[RateRequest], log_errors: bool = True):
    """Price a list of RateRequests and group the responses.

    This contains the rating computation shared by the `/rate_request` endpoint
    and the widget quote flow. It performs **no** persistence — callers decide
    whether to store the result in `Quotes`.

    Returns a tuple ``(grouped, price_bracket)`` where ``price_bracket`` is the
    narrowest ``(priceLow, priceHigh)`` range that contains the request price
    across all successfully-priced options (used for cache keying), or ``None``.
    """
    enriched_results = []
    bracket_low = None
    bracket_high = None

    for req in requests:
        enriched = req.dict()
        try:
            # Add lang field based on locale
            enriched["lang"] = extract_lang_from_locale(req.locale)

            # Validate all fields present and not blank (age=0 is now valid)
            missing = req.missing_fields()
            if missing:
                error = f"Missing or blank required field(s): {', '.join(missing)}"
                if log_errors:
                    error_log_collection.insert_one({
                        "input": req.dict(),
                        "error_type": "validation",
                        "error_detail": error,
                        "created_at": datetime.utcnow()
                    })
                enriched["status"] = "error"
                enriched["error"] = error
                enriched_results.append(enriched)
                continue

            failure_reasons = []
            matching_doc = None

            # Only filter by product_id and currency initially (so we can gather field errors)
            for doc in ratings.find({"currency": req.currency, "productID": {"$in": [req.product_id]}}):
                matched, reasons = match_with_reasons(doc, req)
                if matched:
                    matching_doc = doc
                    break
                else:
                    failure_reasons.append({
                        "doc_id": str(doc["_id"]),
                        "reasons": reasons
                    })

            if not matching_doc:
                error = {
                    "message": "No rating config found matching all input fields.",
                    "details": failure_reasons
                }
                if log_errors:
                    error_log_collection.insert_one({
                        "input": req.dict(),
                        "error_type": "not_found",
                        "error_detail": error,
                        "created_at": datetime.utcnow()
                    })
                enriched["status"] = "error"
                enriched["error"] = error
                enriched_results.append(enriched)
                continue

            base_fee = matching_doc["baseFee"]
            locale_factor = next(
                (f["factor"] for f in matching_doc.get("localeFactor", [])
                 if normalize(f["locale"]) == normalize(req.locale)),
                None
            )
            poc_factor = matching_doc.get("pocFactor", {}).get(str(req.poc))
            category_factor = next(
                (f["factor"] for f in matching_doc.get("categoryFactor", [])
                 if normalize(f["device"]) == normalize(req.category)),
                None
            )
            age_factor = matching_doc.get("ageFactor", {}).get(str(req.age))
            price_factor = find_price_factor(matching_doc.get("priceFactor", []), req.price)
            multi_factor = matching_doc.get("multiFactor", {}).get(str(req.multi_count))

            rate = round(base_fee * locale_factor * poc_factor * category_factor * age_factor * price_factor * multi_factor, 2)
            rounded_price = round_price_49_99(rate)

            # Track the narrowest price bracket containing this price (for caching)
            bracket = find_price_bracket(matching_doc.get("priceFactor", []), req.price)
            if bracket:
                bracket_low = bracket[0] if bracket_low is None else max(bracket_low, bracket[0])
                bracket_high = bracket[1] if bracket_high is None else min(bracket_high, bracket[1])

            enriched["status"] = "ok"
            enriched["factors"] = {
                "base_fee": base_fee,
                "locale_factor": locale_factor,
                "poc_factor": poc_factor,
                "category_factor": category_factor,
                "age_factor": age_factor,
                "price_factor": price_factor,
                "multi_factor": multi_factor
            }
            enriched["rate"] = rate
            enriched["rounded_price"] = rounded_price
            enriched["rounded_price_pence"] = int(round(rounded_price * 100))

        except Exception as e:
            enriched["status"] = "error"
            enriched["error"] = str(e)

        enriched_results.append(enriched)

    # --- Group responses ---
    grouped = group_responses(enriched_results)
    if bracket_low is not None and bracket_high is not None and bracket_low <= bracket_high:
        price_bracket = (bracket_low, bracket_high)
    else:
        price_bracket = None
    return grouped, price_bracket


def store_quote(grouped, device_id=None, client_key=None, extra=None):
    """Persist a grouped quote into the Quotes collection and return its id."""
    quote_doc = {
        "deviceId": device_id,
        "clientKey": client_key,
        "responses": grouped,
        "created_at": datetime.utcnow()
    }
    if extra:
        quote_doc.update(extra)
    quote_insert = quotes_collection.insert_one(quote_doc)
    return str(quote_insert.inserted_id)


# --- Endpoint ---

@router.post(
    "/rate_request",
    summary="Price cover options and store them as a quote",
    response_description="The stored quote id and the priced options, grouped by product.",
    responses=secured({
        200: json_response(
            "Every line was processed. Options carry `status: \"ok\"` when priced and "
            "`status: \"error\"` when no rating configuration matched.",
            {
                "quote_id": "68a1c2d3e4b21d0f8c9e7712",
                "responses": [
                    {
                        "product_id": "ACME-EW-STD",
                        "client": "ACME-UK",
                        "currency": "GBP",
                        "locale": "en_GB",
                        "category": "Dishwasher",
                        "age": 15,
                        "price": 449.99,
                        "multi_count": 1,
                        "source": "web",
                        "options": [
                            {
                                "status": "ok",
                                "poc": 24,
                                "mode": "live",
                                "rate": 71.34,
                                "rounded_price": 71.49,
                                "rounded_price_pence": 7149,
                                "factors": {
                                    "base_fee": 42.0,
                                    "locale_factor": 1.0,
                                    "poc_factor": 1.4,
                                    "category_factor": 1.15,
                                    "age_factor": 1.05,
                                    "price_factor": 1.0,
                                    "multi_factor": 1.0,
                                },
                            },
                            {
                                "status": "error",
                                "poc": 60,
                                "mode": "live",
                                "error": {
                                    "message": "No rating config found matching all input fields.",
                                    "details": [{"doc_id": "6712c0f1a4b21d0f8c9e0031", "reasons": ["poc 60 has no pocFactor"]}],
                                },
                            },
                        ],
                    }
                ],
            },
        ),
    }),
)
def rate_request(
    payload: RateRequestBatch,
    _: None = Depends(verify_token)
):
    """
    Price one or more cover options and persist the result as a quote.

    Each entry in `requests` is matched against the client's `Rating` configuration, and the
    premium is the product of its factors:

    `base_fee × locale_factor × poc_factor × category_factor × age_factor × price_factor × multi_factor`

    The result is rounded up to the nearest `.49` or `.99` — `rounded_price` is what to charge,
    `rounded_price_pence` the same value in minor units for payment providers, and `rate` the raw
    figure before rounding. The `factors` block shows exactly how the price was reached.

    **Responses are grouped, not one-per-request.** Lines sharing a product, client, currency,
    locale, category, age, price, multi-count and source collapse into a single entry, with one
    `options` element per cover term — which is what a "choose 24 or 36 months" UI needs.

    **Per-line failure is not request failure.** A line with no matching rating configuration
    comes back as an option with `status: "error"` and the reasons it failed, while the rest are
    priced normally; the whole call still returns `200`. Failures are logged to
    `Error_Log_RateRequest`.

    Every call **writes a quote** to the `Quotes` collection and returns its `quote_id`, which
    `GET /quote/{quote_id}` and the payment-link endpoints consume. There is no dry-run mode.
    """
    grouped, _bracket = price_and_group(payload.requests)

    # Store grouped responses in Quotes collection
    created_quote_id = store_quote(grouped, device_id=payload.deviceId, client_key=payload.clientKey)

    return {
        "quote_id": created_quote_id,
        "responses": grouped
    }
