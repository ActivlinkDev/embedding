#!/usr/bin/env python3
"""Translate `locales.<locale>.category` on existing MasterSKU documents.

MasterSKUs written before locale categories were translated hold the taxonomy
spelling ("LED Television") in every locale block, so a Spanish or French
journey renders the English category name. This rewrites each locale block's
`category` to the matching `Category.locale_title` entry.

The root `category` is what assignment and rating match on, and is never
touched: it stays the untranslated taxonomy spelling, and the translation is
derived from it.

Two things stop this from destroying the rating key:

* A master with no root `category` is skipped, not guessed at — its locale
  blocks are the only copy of the value, and translating them would lose it.
* A master whose locale blocks disagree with the root (possible on an older
  document that gained a locale after its category was reclassified) is
  reported, because translating the root drops what the locale block said.
  `--include-divergent` proceeds with those; without it they are left alone.

Idempotent: the translation is derived from the root category every time, so a
locale block already holding the right title is left untouched, and a re-run
after a partial run finishes the job.

Usage:
    python scripts/backfill_master_sku_category_titles.py --dry-run
    python scripts/backfill_master_sku_category_titles.py --apply
    python scripts/backfill_master_sku_category_titles.py --apply --include-divergent
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

from utils.category_tree import localized_title, refresh, resolve  # noqa: E402


def divergent_locales(master: dict, root: str) -> list[str]:
    """Locales whose stored category is neither the root's nor its translation.

    A block still holding the untranslated root category is the normal
    pre-backfill state, not a divergence. One naming a *different* category is:
    translating the root would silently drop what that locale said.
    """
    canonical = resolve(root)["category"] or root
    out = []
    for locale, block in sorted((master.get("locales") or {}).items()):
        if not isinstance(block, dict):
            continue
        stored = str(block.get("category") or "").strip()
        if not stored:
            continue
        if stored in (canonical, localized_title(canonical, locale)):
            continue
        out.append(f"{locale}={stored!r}")
    return out


def planned_updates(master: dict, root: str) -> dict:
    """The `$set` operations this master needs, keyed by dotted path."""
    canonical = resolve(root)["category"] or root
    updates: dict[str, str] = {}
    for locale, block in (master.get("locales") or {}).items():
        if not isinstance(block, dict):
            continue
        title = localized_title(canonical, locale)
        if title and title != block.get("category"):
            updates[f"locales.{locale}.category"] = title
    return updates


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="report changes without writing")
    group.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument(
        "--include-divergent",
        action="store_true",
        help="also rewrite masters whose locale blocks name a different category",
    )
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
    no_root: list[str] = []
    divergent: list[str] = []
    with MongoClient(uri, serverSelectionTimeoutMS=15000) as client:
        masters = client[args.database]["MasterSKU"]
        for master in masters.find({}, {"category": 1, "locales": 1}):
            scanned += 1
            root = str(master.get("category") or "").strip()
            if not root:
                no_root.append(str(master["_id"]))
                continue
            diverged = divergent_locales(master, root)
            if diverged:
                divergent.append(f"{master['_id']} root={root!r} {' '.join(diverged)}")
                if not args.include_divergent:
                    continue
            updates = planned_updates(master, root)
            if not updates:
                continue
            changed += 1
            locales_set += len(updates)
            if args.apply:
                masters.update_one({"_id": master["_id"]}, {"$set": updates})
            else:
                print(f"{master['_id']}: {updates}")

    verb = "Updated" if args.apply else "Would update"
    print(f"\nScanned {scanned} MasterSKUs. {verb} {changed} of them ({locales_set} locale blocks).")
    if no_root:
        print(f"\nSkipped {len(no_root)} with no root category (their rating key would be lost):")
        for line in no_root[:20]:
            print(f"  {line}")
    if divergent:
        action = "rewritten anyway" if args.include_divergent else "skipped"
        print(f"\n{len(divergent)} master(s) disagree with their root category ({action}):")
        for line in divergent[:20]:
            print(f"  {line}")
        if not args.include_divergent:
            print("  Re-run with --include-divergent once you have checked these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
