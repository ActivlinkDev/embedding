"""Shared client for the public EPREL (EU energy label registry) API.

Used by scripts/eprel_scrape.py (CLI) and routers/eprel_scrape.py (API).

The API sits behind a WAF that rejects requests without a browser-like
User-Agent AND a Referer on eprel.ec.europa.eu, hence the headers below.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://eprel.ec.europa.eu/api"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://eprel.ec.europa.eu/screen/home",
}
PAGE_LIMIT = 100
MAX_RETRIES = 4


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _get(session: requests.Session, url: str, params: dict | None = None) -> dict | list:
    delay = 2
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=60)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.RequestException(f"HTTP {resp.status_code}")
            resp.raise_for_status()
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            logger.warning("EPREL retry %s/%s after error: %s", attempt, MAX_RETRIES - 1, exc)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def get_product_groups(session: requests.Session) -> list[dict]:
    return _get(session, f"{BASE_URL}/product-groups")


def scrape_group(session: requests.Session, url_code: str, manufacturer: str, delay: float = 0.5) -> list[dict]:
    """Page through one product group and return every matching model."""
    hits: list[dict] = []
    page = 1
    while True:
        data = _get(
            session,
            f"{BASE_URL}/products/{url_code}",
            params={
                "_page": page,
                "_limit": PAGE_LIMIT,
                "supplierOrTrademark": manufacturer,
            },
        )
        batch = data.get("hits", [])
        hits.extend(batch)
        total = data.get("size", 0)
        if not batch or len(hits) >= total:
            return hits
        page += 1
        time.sleep(delay)


def ensure_indexes(coll) -> None:
    from pymongo import ASCENDING

    coll.create_index([("eprelRegistrationNumber", ASCENDING)], unique=True)
    coll.create_index([("supplierOrTrademark", ASCENDING), ("productGroup", ASCENDING)])


def store_in_mongo(coll, products: list[dict], group: str, manufacturer: str) -> tuple[int, int]:
    """Upsert products keyed on eprelRegistrationNumber. Returns (upserted, modified)."""
    from pymongo import ReplaceOne

    ops = []
    scraped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for product in products:
        ern = product.get("eprelRegistrationNumber")
        if ern is None:
            continue
        doc = {
            **product,
            "productGroup": group,
            "manufacturerQuery": manufacturer,
            "scrapedAt": scraped_at,
        }
        ops.append(ReplaceOne({"eprelRegistrationNumber": ern}, doc, upsert=True))
    if not ops:
        return 0, 0
    result = coll.bulk_write(ops, ordered=False)
    return result.upserted_count, result.modified_count
