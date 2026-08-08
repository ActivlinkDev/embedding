#!/usr/bin/env python3
"""Scrape public EPREL (EU energy label registry) data for a given manufacturer.

Queries the public API behind https://eprel.ec.europa.eu across all (or selected)
product groups, filtered by supplier/trademark name, and writes the results to
JSON (one file per product group) plus an optional flattened CSV.

Usage:
    python scripts/eprel_scrape.py "Samsung"
    python scripts/eprel_scrape.py "Bosch" --groups washingmachines2019,dishwashers2019
    python scripts/eprel_scrape.py "LG" --csv --out ./eprel_lg
    python scripts/eprel_scrape.py "Samsung" --mongo                 # upsert into MongoDB
    python scripts/eprel_scrape.py "Samsung" --mongo --no-files      # MongoDB only

MongoDB:
    --mongo upserts every model into a collection (default EPREL_Products in the
    Activlink db), keyed on eprelRegistrationNumber, so re-running a scrape
    updates existing documents instead of duplicating them. Connection comes
    from the MONGO_URI env var (a .env file is honoured), db from MONGO_DB.

Notes:
    - The API sits behind a WAF that rejects requests without a browser-like
      User-Agent AND a Referer on eprel.ec.europa.eu, hence the headers below.
    - The supplierOrTrademark filter matches both the registering organisation
      and the trademark, case-insensitively.
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
            print(f"    retry {attempt}/{MAX_RETRIES - 1} after error: {exc}", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def get_product_groups(session: requests.Session) -> list[dict]:
    return _get(session, f"{BASE_URL}/product-groups")


def scrape_group(session: requests.Session, url_code: str, manufacturer: str, delay: float) -> list[dict]:
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


def flatten_for_csv(product: dict, group: str) -> dict:
    org = product.get("organisation") or {}
    return {
        "productGroup": group,
        "eprelRegistrationNumber": product.get("eprelRegistrationNumber"),
        "modelIdentifier": product.get("modelIdentifier"),
        "supplier": org.get("organisationName"),
        "trademark": product.get("supplierOrTrademark") or product.get("trademarkOwner"),
        "energyClass": product.get("energyClass") or product.get("energyClassImage"),
        "status": product.get("status"),
        "onMarketStartDateTS": product.get("onMarketStartDateTS"),
        "onMarketEndDateTS": product.get("onMarketEndDateTS"),
        "implementingAct": product.get("implementingAct"),
    }


def get_mongo_collection(collection_name: str):
    """Connect using the repo's MONGO_URI / MONGO_DB conventions."""
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        sys.exit("MONGO_URI is not set (add it to your environment or .env file)")
    from pymongo import ASCENDING, MongoClient

    client = MongoClient(mongo_uri)
    db = client[os.getenv("MONGO_DB", "Activlink")]
    coll = db[collection_name]
    coll.create_index([("eprelRegistrationNumber", ASCENDING)], unique=True)
    coll.create_index([("supplierOrTrademark", ASCENDING), ("productGroup", ASCENDING)])
    return coll


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape EPREL data for a manufacturer")
    parser.add_argument("manufacturer", help="Supplier or trademark name, e.g. 'Samsung'")
    parser.add_argument("--groups", help="Comma-separated product group url codes (default: all)")
    parser.add_argument("--out", default="eprel_data", help="Output directory (default: eprel_data)")
    parser.add_argument("--csv", action="store_true", help="Also write a flattened summary CSV")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between requests (default: 0.5)")
    parser.add_argument("--mongo", action="store_true", help="Upsert results into MongoDB (uses MONGO_URI)")
    parser.add_argument("--collection", default="EPREL_Products",
                        help="MongoDB collection name (default: EPREL_Products)")
    parser.add_argument("--no-files", action="store_true", help="Skip writing JSON/CSV files")
    args = parser.parse_args()

    if args.no_files and not args.mongo:
        parser.error("--no-files requires --mongo (otherwise nothing is stored)")

    mongo_coll = get_mongo_collection(args.collection) if args.mongo else None

    session = requests.Session()
    session.headers.update(HEADERS)

    all_groups = get_product_groups(session)
    known = {g["url_code"]: g for g in all_groups}
    if args.groups:
        wanted = [g.strip() for g in args.groups.split(",") if g.strip()]
        unknown = [g for g in wanted if g not in known]
        if unknown:
            parser.error(f"unknown product group(s): {', '.join(unknown)}. "
                         f"Valid: {', '.join(sorted(known))}")
        groups = [known[g] for g in wanted]
    else:
        groups = all_groups

    write_files = not args.no_files
    out_dir = Path(args.out)
    if write_files:
        out_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, int] = {}
    csv_rows: list[dict] = []
    mongo_upserted = mongo_modified = 0
    for group in groups:
        code = group["url_code"]
        print(f"Scraping {group['name']} ({code}) ...")
        products = scrape_group(session, code, args.manufacturer, args.delay)
        summary[code] = len(products)
        print(f"    {len(products)} models")
        if products:
            if write_files:
                with open(out_dir / f"{code}.json", "w", encoding="utf-8") as fh:
                    json.dump(products, fh, ensure_ascii=False, indent=2)
                csv_rows.extend(flatten_for_csv(p, code) for p in products)
            if mongo_coll is not None:
                upserted, modified = store_in_mongo(mongo_coll, products, code, args.manufacturer)
                mongo_upserted += upserted
                mongo_modified += modified
                print(f"    mongo: {upserted} new, {modified} updated")
        time.sleep(args.delay)

    if write_files:
        with open(out_dir / "summary.json", "w", encoding="utf-8") as fh:
            json.dump(
                {"manufacturer": args.manufacturer,
                 "scrapedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "totals": summary,
                 "grandTotal": sum(summary.values())},
                fh, ensure_ascii=False, indent=2,
            )

        if args.csv and csv_rows:
            with open(out_dir / "summary.csv", "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(csv_rows[0].keys()))
                writer.writeheader()
                writer.writerows(csv_rows)

    total = sum(summary.values())
    print(f"\nDone. {total} models across {sum(1 for v in summary.values() if v)} product groups.")
    if write_files:
        print(f"Files written to {out_dir}/")
    if mongo_coll is not None:
        print(f"MongoDB '{args.collection}': {mongo_upserted} inserted, {mongo_modified} updated")


if __name__ == "__main__":
    main()
