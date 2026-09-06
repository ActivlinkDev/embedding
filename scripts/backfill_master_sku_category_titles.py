#!/usr/bin/env python3
"""Backfill `locales.<locale>.categoryTitle` on existing MasterSKU documents.

MasterSKUs written before locale category titles existed carry only the taxonomy
spelling ("LED Television") in every locale block, so a Spanish or French journey
renders the English category name. This fills each locale block's
`categoryTitle` from the matching `Category.locale_title` entry.

`category` is left exactly as it is: it is the key assignment rules and rating
tables match on, and localizing it would mis-rate every non-English journey.

Idempotent, and safe to re-run: a locale block whose `categoryTitle` already
matches what the taxonomy would give is left untouched.

Usage:
    python scripts/backfill_master_sku_category_titles.py --dry-run
    python scripts/backfill_master_sku_category_titles.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from utils.category_tree import localized_title, refresh  # noqa: E402


def planned_updates(master: dict) -> dict:
    """The `$set` operations this master needs, keyed by dotted path."""
    updates: dict[str, str] = {}
    for locale, block in (master.get("locales") or {}).items():
        if not isinstance(block, dict):
            continue
        category = str(block.get("category") or master.get("category") or "").strip()
        if not category:
            continue
        title = localized_title(category, locale)
        if title and title != block.get("categoryTitle"):
            updates[f"locales.{locale}.categoryTitle"] = title
    return updates


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="report changes without writing")
    group.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--database", default=os.getenv("MONGO_DB", "Activlink"))
    args = parser.parse_args()

    uri = os.getenv("MONGO_URI")
    if not uri:
        print("MONGO_URI is not configured", file=sys.stderr)
        return 2

    from pymongo import MongoClient

    loaded = refresh()
    if not loaded:
        print("The Category taxonomy is empty or unreachable; refusing to run", file=sys.stderr)
        return 2
    print(f"Loaded {loaded} categories from the taxonomy")

    scanned = changed = locales_set = 0
    with MongoClient(uri, serverSelectionTimeoutMS=15000) as client:
        masters = client[args.database]["MasterSKU"]
        for master in masters.find({}, {"category": 1, "locales": 1}):
            scanned += 1
            updates = planned_updates(master)
            if not updates:
                continue
            changed += 1
            locales_set += len(updates)
            if args.apply:
                masters.update_one({"_id": master["_id"]}, {"$set": updates})
            else:
                print(f"{master['_id']}: {updates}")

    verb = "Updated" if args.apply else "Would update"
    print(f"Scanned {scanned} MasterSKUs. {verb} {changed} of them ({locales_set} locale blocks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
