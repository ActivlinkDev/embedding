#!/usr/bin/env python3
"""Generate the fault catalogue for every category, ahead of any customer hitting it.

`POST /faults/catalogue` generates on first use and caches, so without seeding the first
customer to report a fault on a given category waits for a language-model call — and, if
that call fails, gets no fault list at all and has to describe the problem in free text.
This walks the `Category` taxonomy (tens of documents) and warms every entry.

WHAT IT WRITES
--------------
One `FaultCatalogue` document per category, carrying one `Content` entry per locale. Each
entry's faults are classified into the fixed vocabulary in `routers/faults/fault_types.py`
— the script never invents a type.

SAFETY
------
An existing (category, locale) is skipped unless `--force`, so re-running after adding a
locale costs only the new locale. Entries marked `source: "curated"` are **never**
regenerated, even with `--force`, so hand-written copy survives. Nothing is deleted: a
category in the taxonomy that generation fails on is reported and left absent, to be
picked up by the next run or by the endpoint's own first-use generation.

COST
----
One model call per (category, locale). Tens of categories times the locales in use, once.
Use `--dry-run` first to see the count.

Usage:
    python scripts/seed_fault_catalogue.py --locales en_GB --dry-run
    python scripts/seed_fault_catalogue.py --locales en_GB,es_ES
    python scripts/seed_fault_catalogue.py --locales fr_FR --categories Dishwasher,Oven
    python scripts/seed_fault_catalogue.py --locales en_GB --force
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from routers.faults.catalogue import (  # noqa: E402
    SOURCE_CURATED,
    catalogue_collection,
    ensure_fault_catalogue_indexes,
    find_locale_content,
    generate_catalogue,
    store_catalogue,
)
from utils.category_tree import all_categories  # noqa: E402

LOCALE_PATTERN = r"^[a-z]{2}_[A-Z]{2}$"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--locales", required=True, help="Comma-separated FastAPI locales, e.g. en_GB,es_ES")
    parser.add_argument("--categories", help="Comma-separated categories. Default: every category in the taxonomy.")
    parser.add_argument("--force", action="store_true", help="Regenerate entries that already exist (curated ones are still skipped).")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be generated, call nothing, write nothing.")
    return parser.parse_args(argv)


def resolve_categories(requested: str | None) -> list[str]:
    """The taxonomy spellings to seed.

    A name passed with --categories is checked against the taxonomy rather than trusted:
    the catalogue is keyed on the taxonomy spelling, and seeding a category the taxonomy
    does not carry would produce a document no lookup ever matches.
    """
    known = [entry["category"] for entry in all_categories() if entry.get("category")]
    if not requested:
        return known
    wanted = [name.strip() for name in requested.split(",") if name.strip()]
    lookup = {name.casefold(): name for name in known}
    resolved, missing = [], []
    for name in wanted:
        match = lookup.get(name.casefold())
        (resolved.append(match) if match else missing.append(name))
    for name in missing:
        print(f"  ! not in the Category taxonomy, skipped: {name}")
    return resolved


def main(argv=None) -> int:
    args = parse_args(argv)
    locales = [value.strip() for value in args.locales.split(",") if value.strip()]
    for locale in locales:
        if not re.match(LOCALE_PATTERN, locale):
            print(f"! '{locale}' is not a FastAPI locale (expected e.g. en_GB)")
            return 2

    categories = resolve_categories(args.categories)
    if not categories:
        print("! no categories to seed — is the Category taxonomy readable?")
        return 1

    print(f"{len(categories)} categories x {len(locales)} locales = {len(categories) * len(locales)} pairs")
    if args.dry_run:
        print("(dry run — nothing will be generated or written)")
    else:
        ensure_fault_catalogue_indexes()

    generated = skipped = failed = 0
    for category in categories:
        doc = catalogue_collection.find_one({"Category": category}) if not args.dry_run else None
        for locale in locales:
            existing = find_locale_content(doc, locale)
            if existing and existing.get("source") == SOURCE_CURATED:
                print(f"  = curated, left alone: {category} / {locale}")
                skipped += 1
                continue
            if existing and not args.force:
                skipped += 1
                continue
            if args.dry_run:
                print(f"  + would generate: {category} / {locale}")
                generated += 1
                continue
            try:
                faults, labels = generate_catalogue(category, locale)
            except Exception as exc:  # noqa: BLE001 — one bad category must not stop the run
                print(f"  ! generation failed, left for next run: {category} / {locale}: {exc}")
                failed += 1
                continue
            store_catalogue(category, locale, faults, labels)
            print(f"  + {category} / {locale}: {len(faults)} faults")
            generated += 1

    print(f"\ngenerated {generated}, skipped {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
