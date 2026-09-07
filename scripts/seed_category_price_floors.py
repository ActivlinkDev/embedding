#!/usr/bin/env python3
"""Seed `min_market_price` on the Category taxonomy.

The floor is what `utils.category_tree.min_market_price` reads and the
DataforSEO webhook checks a matched price against. Until a category carries
one, that guard is inert: enrichment matched a Beko dishwasher against an eBay
spare-parts listing and stored 11.99 GBP as `market.referencePrice`, which is
what the quote rates the cover on.

WHAT THESE NUMBERS ARE
----------------------
Spare-part detectors, not market-price estimates. Each floor sits well below
the cheapest genuine new unit for its category and well above what an
accessory, filter, drawer or cable sells for — roughly a third of an
entry-level price. That gap is the whole design: a floor set too low merely
does nothing, while one set too high suppresses enrichment for real products
that happen to be cheap. When in doubt, go lower.

They are seed values, chosen from typical retail prices rather than measured
from the catalogue. Review them against what your SKUs actually cost before
applying, and treat this table as the place to edit them afterwards.

CURRENCIES
----------
GBP and EUR only. `min_market_price` treats a currency the map does not name
as having no floor, so tr_TR enrichment stays unguarded until someone adds TRL
at a rate they have checked — a stale rate here would be worse than nothing,
rejecting every Turkish match if it were too high. Add it to `_floors` when
you have a current one.

SAFETY
------
Never creates a Category document: a name in the table that the taxonomy does
not carry is reported, not inserted. The taxonomy's `category` is the key
rating matches on, and inventing one would produce a category nothing can
rate. Existing floors are left alone unless `--overwrite` is passed, so a
hand-tuned value survives a re-run.

Usage:
    python scripts/seed_category_price_floors.py --dry-run
    python scripts/seed_category_price_floors.py --apply
    python scripts/seed_category_price_floors.py --apply --overwrite
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

from utils.category_tree import _key  # noqa: E402

# EUR floors are derived rather than listed twice, so editing a category means
# editing one number. The rate is deliberately coarse: these are thresholds an
# order of magnitude away from the prices they reject, not conversions.
EUR_PER_GBP = 1.15

# category -> floor in GBP, for a plausible *new* unit of the cheapest kind.
FLOORS_GBP = {
    # Major kitchen appliances. Spare parts (drawers, baskets, seals, pumps)
    # run 5-60 GBP, which is what the Beko case matched.
    "Dishwasher": 80,
    "Washing Machine": 80,
    "Washer Dryer": 100,
    "Tumble Dryer": 80,
    "Refrigerator Freezer": 80,
    "Refrigerator": 80,
    "Freezer": 80,
    "Oven": 70,
    "Cooker": 100,
    "Range Cooker": 100,
    "Hob": 45,
    "Cooker Hood": 30,
    "Microwave": 25,
    "Commercial Refrigerator": 200,
    "Professional Refrigerated Cabinet": 200,
    # Heating, cooling and water. Space heaters are genuinely cheap, so the
    # floor is low enough to leave a 20 GBP fan heater alone.
    "Boiler": 150,
    "Water Heater": 40,
    "Heater": 12,
    "Air Conditioner": 60,
    "Ventilation Unit": 30,
    # Consumer electronics.
    "Television": 50,
    "LED Television": 50,
    "Laptop": 80,
    "Desktop Computer": 80,
    "Monitor": 30,
    "Smartphone": 40,
    "Tablet": 40,
    "Smartphone Tablet": 40,
    "Games Console": 50,
    "Camera": 35,
    "Printer": 20,
    # Small domestic appliances. These sit close to accessory prices by nature,
    # so the floors are correspondingly small — a kettle really can cost 8 GBP.
    "Vacuum Cleaner": 18,
    "Air Fryer": 15,
    "Coffee Machine": 12,
    "Food Processor": 8,
    "Blender": 8,
    "Kettle": 6,
    "Toaster": 6,
    "Iron": 6,
    "Hair Dryer": 6,
}

# Categories with no useful floor are left out on purpose rather than given a
# token one: a consumable costs about what its packaging does, so no threshold
# separates the product from an accessory. Light Bulb is the clearest case.


def _floors(gbp: float) -> dict[str, float]:
    """The per-currency map stored on a category, from one GBP figure."""
    return {"GBP": float(gbp), "EUR": float(round(gbp * EUR_PER_GBP))}


def plan(existing: list[dict], overwrite: bool) -> tuple[list[tuple], list[str], list[str]]:
    """Work out what to write, without touching the database.

    Returns ``(updates, unknown, unfloored)``: the writes to apply as
    ``(category, floors)``; table entries the taxonomy does not carry; and
    taxonomy categories this table says nothing about. The last two are
    reported rather than acted on — one would mean inventing a category, the
    other guessing a floor.
    """
    by_key = {_key(name): gbp for name, gbp in FLOORS_GBP.items()}
    seen: set[str] = set()
    updates: list[tuple] = []
    unfloored: list[str] = []

    for doc in existing:
        name = str(doc.get("category") or "").strip()
        if not name:
            continue
        key = _key(name)
        gbp = by_key.get(key)
        if gbp is None:
            unfloored.append(name)
            continue
        seen.add(key)
        if doc.get("min_market_price") and not overwrite:
            continue
        updates.append((name, _floors(gbp)))

    unknown = sorted(name for name, _ in FLOORS_GBP.items() if _key(name) not in seen)
    return updates, unknown, sorted(unfloored)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="report changes without writing")
    group.add_argument("--apply", action="store_true", help="write the floors")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace floors already stored (default: leave hand-tuned values alone)",
    )
    parser.add_argument("--database", default=os.getenv("MONGO_DB", "Activlink"))
    args = parser.parse_args()

    uri = os.getenv("MONGO_URI")
    if not uri:
        print("MONGO_URI is not configured", file=sys.stderr)
        return 2

    from pymongo import MongoClient

    with MongoClient(uri, serverSelectionTimeoutMS=15000) as client:
        categories = client[args.database]["Category"]
        existing = list(categories.find({}, {"category": 1, "min_market_price": 1}))
        if not existing:
            print("The Category collection is empty or unreachable; refusing to run", file=sys.stderr)
            return 2

        updates, unknown, unfloored = plan(existing, args.overwrite)

        for name, floors in updates:
            print(f"  {name:38} GBP {floors['GBP']:>7.0f}   EUR {floors['EUR']:>7.0f}")
            if args.apply:
                categories.update_one(
                    {"category": name}, {"$set": {"min_market_price": floors}}
                )

    verb = "Set" if args.apply else "Would set"
    print(f"\n{verb} floors on {len(updates)} of {len(existing)} categories.")
    if not args.overwrite:
        print("Categories that already carry a floor were left alone (--overwrite to replace).")
    if unfloored:
        print(
            f"\n{len(unfloored)} taxonomy categories have no floor in this table and are "
            "therefore unguarded — enrichment stores whatever price it finds for them:"
        )
        for name in unfloored:
            print(f"  {name}")
        print("  Add the ones that matter to FLOORS_GBP; leave consumables out.")
    if unknown:
        print(
            f"\n{len(unknown)} entries in this table name no taxonomy category and were "
            "ignored (nothing was created):"
        )
        for name in unknown:
            print(f"  {name}")
        print("  Correct the spelling to match the taxonomy, or drop the entry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
