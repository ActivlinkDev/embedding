#!/usr/bin/env python3
"""Seed the EPREL_Category_Map collection with default product-group mappings.

Idempotent: existing documents keep any hand-edited `category` / `enabled`
values, so this is safe to re-run whenever EPREL adds a product group.

Usage:
    python scripts/seed_eprel_category_map.py
    python scripts/seed_eprel_category_map.py --list   # show current mappings
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.eprel_mapping import DEFAULT_GROUP_NAMES  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def get_collection():
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        sys.exit("MONGO_URI is not set (add it to your environment or .env file)")
    from pymongo import ASCENDING, MongoClient

    client = MongoClient(mongo_uri)
    db = client[os.getenv("MONGO_DB", "Activlink")]
    coll = db["EPREL_Category_Map"]
    coll.create_index([("productGroup", ASCENDING)], unique=True)
    return coll


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed EPREL_Category_Map")
    parser.add_argument("--list", action="store_true", help="List current mappings and exit")
    args = parser.parse_args()

    coll = get_collection()

    if args.list:
        for doc in coll.find({}, {"_id": 0}).sort("productGroup", 1):
            status = "" if doc.get("enabled", True) else "  [disabled]"
            print(f"{doc['productGroup']:45} {doc.get('name',''):35} "
                  f"-> {doc.get('category') or '(unresolved)'}{status}")
        return

    inserted = 0
    for product_group, name in sorted(DEFAULT_GROUP_NAMES.items()):
        result = coll.update_one(
            {"productGroup": product_group},
            {"$setOnInsert": {
                "productGroup": product_group,
                "name": name,
                "category": None,
                "enabled": True,
            }},
            upsert=True,
        )
        if result.upserted_id is not None:
            inserted += 1

    total = coll.count_documents({})
    print(f"Seeded EPREL_Category_Map: {inserted} new, {total} total mappings.")
    print("Existing documents were left untouched. Run with --list to review, and pin a "
          "canonical category per group (or via PUT /eprel/category-map/{product_group}).")


if __name__ == "__main__":
    main()
