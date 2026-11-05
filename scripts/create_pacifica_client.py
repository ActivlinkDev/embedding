#!/usr/bin/env python3
"""Create the Pacifica client: one `ClientKey` document with Pacifica's branding.

A client is a document in the `ClientKey` collection. `GET /get-client` returns
it to the frontend, the portal lists it by `Client_ID`, and its `Styles` object
drives the branding of every customer-facing page (see the frontend's
lib/themeTokens.ts for how each token is used).

THE CLIENT
----------
    ClientKey    PAC82627   (what goes in the journey URL as ?clientkey=)
    Client_ID    PACIFICA   (what portal users are scoped to)
    Client_Name  Pacifica
    Source       web

BRANDING
--------
Taken from https://pacifica.co.uk/ (November 2026): the logo is navy #182641
with a green #03AC84 and coral #E1554D mark; the site's primary buttons are green
#03AC83 and turn navy #192641 on hover; body text is navy. Surfaces use the
neutral journey palette (as in neutralize_client_backgrounds.py) so the brand
colour is kept for actions.

White text on the brand green is 2.9:1, which is what pacifica.co.uk itself uses
for its buttons. Link text and error text, which are smaller and sit on white,
use darker shades instead (#027A5D at 5.3:1, #C8423A at 4.9:1).

The logo is hot-linked from pacifica.co.uk. If Pacifica move it, the header falls
back to no logo; upload a copy to the CMS and change `Styles.Logo_URL` (via the
portal's Client Styles tab) to remove that dependency.

SAFETY
------
- Dry run unless `--apply` is passed: it prints the document and writes nothing.
- Refuses to write if a document with this ClientKey or this Client_ID already
  exists, so re-running cannot create a duplicate or overwrite an existing client.
- Inserts one document into `ClientKey`; touches nothing else.

UNDO
----
    db.ClientKey.deleteOne({ClientKey: "PAC82627", Client_ID: "PACIFICA"})

Usage:
    python scripts/create_pacifica_client.py            # dry run
    python scripts/create_pacifica_client.py --apply

Needs MONGO_URI (read from the environment or a .env file). `--db` defaults to
the API's database, Activlink.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

COLLECTION = "ClientKey"

CLIENT_KEY = "PAC82627"
CLIENT_ID = "PACIFICA"

PACIFICA_GREEN = "#03AC83"
PACIFICA_NAVY = "#192641"

STYLES: dict[str, str] = {
    # Surfaces: the neutral journey palette.
    "PrimaryBackground": "#F6F6F4",
    "SecondaryBackground": "#FFFFFF",
    "CardBackground": "#FFFFFF",
    "InputBackground": "#FFFFFF",
    "HeaderMenuBackground": "#FFFFFF",
    "Border": "#E7E6E2",
    "CardBorder": "#E7E6E2",
    "InputBorder": "#D5D3CE",
    # Text: Pacifica navy, with lighter navy-greys for secondary and muted copy.
    "Font": PACIFICA_NAVY,
    "FontSecondary": "#4A5468",
    "FontMuted": "#6B7385",
    # Actions: green buttons that turn navy on hover, as on pacifica.co.uk.
    "Accent": PACIFICA_GREEN,
    "ButtonBackground": PACIFICA_GREEN,
    "ButtonText": "#FFFFFF",
    "ButtonHover": PACIFICA_NAVY,
    "Highlight": "#027A5D",
    "Success": PACIFICA_GREEN,
    "Error": "#C8423A",
    # Header: white bar, navy links, green on hover.
    "HeaderBackground": "#FFFFFF",
    "HeaderText": PACIFICA_NAVY,
    "HeaderBorder": "#E7E6E2",
    "HeaderAccent": PACIFICA_GREEN,
    "HeaderLink": PACIFICA_NAVY,
    "HeaderLinkHover": PACIFICA_GREEN,
    "Logo_URL": "https://pacifica.co.uk/wp-content/uploads/2024/02/logo.svg",
}

CLIENT_DOC: dict = {
    "ClientKey": CLIENT_KEY,
    "Client_ID": CLIENT_ID,
    "Client_Name": "Pacifica",
    "Source": "web",
    "Styles": STYLES,
}


def conflicts(col) -> list[str]:
    """Why the client cannot be created, or an empty list if it can."""
    found = []
    if col.find_one({"ClientKey": CLIENT_KEY}, {"_id": 1}):
        found.append(f"a client with ClientKey {CLIENT_KEY!r} already exists")
    if col.find_one({"Client_ID": CLIENT_ID}, {"_id": 1}):
        found.append(f"a client with Client_ID {CLIENT_ID!r} already exists")
    return found


def create(col, apply: bool) -> int:
    print(json.dumps(CLIENT_DOC, indent=2))
    problems = conflicts(col)
    if problems:
        for p in problems:
            print(f"Refusing to write: {p}.", file=sys.stderr)
        return 1
    if not apply:
        print("\nDry run: nothing written. Pass --apply to create this client.")
        return 0
    result = col.insert_one(dict(CLIENT_DOC))
    print(f"\nCreated {CLIENT_KEY} (_id {result.inserted_id}).")
    return 0


def _connect(db_name: str):
    from utils.mongo import require_db

    return require_db(db_name)[COLLECTION]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="create the client (default is a dry run)")
    parser.add_argument("--db", default="Activlink", help="database name (default: Activlink, as the API uses)")
    args = parser.parse_args(argv)
    return create(_connect(args.db), args.apply)


if __name__ == "__main__":
    sys.exit(main())
