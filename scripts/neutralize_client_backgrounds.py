#!/usr/bin/env python3
"""Set every client's background and border colours to the neutral journey palette.

Each client's branding lives on its `ClientKey` document under `Styles` (what the
portal's Client Styles tab writes, via PUT /portal/styles). Clients have tinted
their surfaces — AO's pale green cards and borders, for instance — which washes
the whole customer journey in the brand colour. The frontend now draws the
journey on neutral surfaces with the brand accent kept for actions; this script
brings the stored data into line, so the pages that still read these tokens
(lookup, landing, assistance, my-contracts, the customer hub) match.

WHAT IT CHANGES
---------------
Only the surface tokens in `NEUTRAL_SURFACES` below. Accent, button, font,
header, logo, success/error and every other key are left exactly as they are.
Each field is written with its own `$set` on `Styles.<field>`, never by
replacing `Styles`, so nothing else on the document can be lost.

By default a token is written whether or not the client set it, because an
unset token falls back to the frontend's warm default palette rather than the
neutral one. Pass `--only-existing` to touch only tokens a client already set.

SAFETY
------
- Dry run unless `--apply` is passed: it prints, per client, each token's
  current and proposed value and writes nothing.
- Before any write, the complete current `Styles` of every client about to
  change is saved to a timestamped JSON file under `--backup-dir`. If that
  file cannot be written, nothing is changed.
- Only the `ClientKey` collection is read or written. Documents are matched by
  `_id` and `ClientKey` together.
- `--client-key` (repeatable) limits the run to named clients — try one first.
- Re-running is harmless: a token already at its neutral value is skipped.

UNDO
----
`--restore <backup.json>` puts back each client's `Styles` from a backup. It is
a dry run too unless `--apply` is passed. Restore replaces the whole `Styles`
object with the backed-up one, so any branding change made after the backup
would be lost; the dry run lists which clients differ from their backup in
fields this script does not touch, so you can see that before applying.

Usage:
    python scripts/neutralize_client_backgrounds.py                         # dry run, all clients
    python scripts/neutralize_client_backgrounds.py --client-key AO12345    # dry run, one client
    python scripts/neutralize_client_backgrounds.py --client-key AO12345 --apply
    python scripts/neutralize_client_backgrounds.py --apply                 # all clients
    python scripts/neutralize_client_backgrounds.py --restore backups/client_styles_<ts>.json
    python scripts/neutralize_client_backgrounds.py --restore backups/client_styles_<ts>.json --apply

Needs MONGO_URI (read from the environment or a .env file). `--db` defaults to
the API's database, Activlink.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# The neutral palette, matching the frontend's journey tokens (tailwind.config.ts:
# canvas, surface, line, line.strong). The surfaces split two ways across the
# frontend: PrimaryBackground is the page itself, and SecondaryBackground is the
# card/panel colour on the app-shell pages (lookup, landing), so the page gets the
# light canvas and every panel and card is white.
NEUTRAL_SURFACES: dict[str, str] = {
    "PrimaryBackground": "#F6F6F4",
    "SecondaryBackground": "#FFFFFF",
    "CardBackground": "#FFFFFF",
    "InputBackground": "#FFFFFF",
    "HeaderMenuBackground": "#FFFFFF",
    "Border": "#E7E6E2",
    "CardBorder": "#E7E6E2",
    "InputBorder": "#D5D3CE",
}

COLLECTION = "ClientKey"


def _same_colour(a: Any, b: Any) -> bool:
    """Compare stored colours loosely: `#ffffff` and ` #FFFFFF ` are the same value."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return a.strip().lower() == b.strip().lower()


def plan_client(doc: dict, only_existing: bool = False) -> list[tuple[str, Any, str]]:
    """The writes one client needs, as `(field, current, proposed)`. Pure; touches no database.

    A document whose `Styles` is missing gets the full set (written as
    `Styles.<field>`, which creates the object). One whose `Styles` is present
    but not an object is skipped — writing into it would fail — and is
    reported by the caller.
    """
    styles = doc.get("Styles")
    if styles is None:
        styles = {}
    if not isinstance(styles, dict):
        return []
    changes: list[tuple[str, Any, str]] = []
    for field, proposed in NEUTRAL_SURFACES.items():
        present = field in styles
        current = styles.get(field)
        if only_existing and not present:
            continue
        if _same_colour(current, proposed):
            continue
        changes.append((field, current if present else None, proposed))
    return changes


def update_for(changes: list[tuple[str, Any, str]]) -> dict:
    """The `$set` for one client's changes — one dotted path per field, never all of `Styles`."""
    return {"$set": {f"Styles.{field}": proposed for field, _, proposed in changes}}


def untouched_differences(backed_up: Any, current: Any) -> list[str]:
    """Fields outside NEUTRAL_SURFACES that differ between a backup and today's `Styles`.

    Restoring replaces the whole `Styles` object, so these are the edits a restore
    would undo that this script never made.
    """
    old = backed_up if isinstance(backed_up, dict) else {}
    new = current if isinstance(current, dict) else {}
    keys = (set(old) | set(new)) - set(NEUTRAL_SURFACES)
    return sorted(k for k in keys if old.get(k) != new.get(k))


def _describe(value: Any) -> str:
    return "(unset)" if value is None else repr(value)


def _connect(db_name: str):
    from utils.mongo import require_db

    return require_db(db_name)[COLLECTION]


def _selector(client_keys: list[str]) -> dict:
    return {"ClientKey": {"$in": client_keys}} if client_keys else {}


def run_neutralize(args: argparse.Namespace) -> int:
    col = _connect(args.db)
    docs = list(col.find(_selector(args.client_key), {"ClientKey": 1, "Styles": 1}))
    if args.client_key:
        found = {d.get("ClientKey") for d in docs}
        for missing in sorted(set(args.client_key) - found):
            print(f"! {missing}: no ClientKey document with this key")

    planned: list[tuple[dict, list[tuple[str, Any, str]]]] = []
    for doc in sorted(docs, key=lambda d: str(d.get("ClientKey"))):
        key = doc.get("ClientKey") or f"<no ClientKey, _id {doc.get('_id')}>"
        if doc.get("Styles") is not None and not isinstance(doc.get("Styles"), dict):
            print(f"! {key}: Styles is a {type(doc['Styles']).__name__}, not an object — skipped")
            continue
        changes = plan_client(doc, only_existing=args.only_existing)
        if not changes:
            print(f"= {key}: already neutral")
            continue
        planned.append((doc, changes))
        print(f"~ {key}:")
        for field, current, proposed in changes:
            print(f"    {field:<22} {_describe(current):<14} -> {proposed!r}")

    total = sum(len(c) for _, c in planned)
    print(f"\n{len(planned)} of {len(docs)} client(s) to update, {total} field(s) in all.")
    if not planned:
        return 0
    if not args.apply:
        print("Dry run: nothing written. Re-run with --apply to write these changes.")
        return 0

    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"client_styles_{stamp}.json"
    backup = [
        {"_id": str(doc["_id"]), "ClientKey": doc.get("ClientKey"), "Styles": doc.get("Styles")}
        for doc, _ in planned
    ]
    # Written and flushed before the first update; if this fails the run stops here.
    with open(backup_path, "w", encoding="utf-8") as fh:
        json.dump({"taken_at": stamp, "db": args.db, "clients": backup}, fh, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    print(f"Backup of {len(backup)} client(s) written to {backup_path}")

    updated = 0
    for doc, changes in planned:
        result = col.update_one({"_id": doc["_id"], "ClientKey": doc.get("ClientKey")}, update_for(changes))
        if result.matched_count != 1:
            print(f"! {doc.get('ClientKey')}: document changed or vanished during the run — not updated")
            continue
        updated += 1
    print(f"Updated {updated} of {len(planned)} client(s). Undo with: --restore {backup_path} --apply")
    return 0 if updated == len(planned) else 1


def run_restore(args: argparse.Namespace) -> int:
    with open(args.restore, encoding="utf-8") as fh:
        payload = json.load(fh)
    entries = payload.get("clients") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        print(f"! {args.restore} is not a backup written by this script")
        return 1
    if args.client_key:
        entries = [e for e in entries if e.get("ClientKey") in set(args.client_key)]

    from bson import ObjectId

    col = _connect(args.db)
    restorable = []
    for entry in entries:
        key = entry.get("ClientKey")
        try:
            oid = ObjectId(entry["_id"])
        except Exception:
            print(f"! {key}: backup _id {entry.get('_id')!r} is not an ObjectId — skipped")
            continue
        doc = col.find_one({"_id": oid, "ClientKey": key}, {"Styles": 1})
        if doc is None:
            print(f"! {key}: no longer in the database — skipped")
            continue
        others = untouched_differences(entry.get("Styles"), doc.get("Styles"))
        note = f" (also reverts later edits to: {', '.join(others)})" if others else ""
        print(f"~ {key}: restore Styles from backup{note}")
        restorable.append((oid, key, entry.get("Styles")))

    print(f"\n{len(restorable)} client(s) to restore.")
    if not args.apply:
        print("Dry run: nothing written. Re-run with --apply to restore.")
        return 0
    restored = 0
    for oid, key, styles in restorable:
        update = {"$unset": {"Styles": ""}} if styles is None else {"$set": {"Styles": styles}}
        if col.update_one({"_id": oid, "ClientKey": key}, update).matched_count == 1:
            restored += 1
    print(f"Restored {restored} of {len(restorable)} client(s).")
    return 0 if restored == len(restorable) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    parser.add_argument("--client-key", action="append", default=[], help="limit to this ClientKey (repeatable)")
    parser.add_argument("--only-existing", action="store_true", help="only change tokens a client already set")
    parser.add_argument("--backup-dir", default="backups", help="where the pre-change backup is written (default: backups)")
    parser.add_argument("--restore", metavar="BACKUP_JSON", help="restore Styles from a backup instead")
    parser.add_argument("--db", default="Activlink", help="database name (default: Activlink, as the API uses)")
    args = parser.parse_args(argv)
    return run_restore(args) if args.restore else run_neutralize(args)


if __name__ == "__main__":
    sys.exit(main())
