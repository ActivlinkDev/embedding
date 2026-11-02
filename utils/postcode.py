"""Postcodes: normalising what a customer typed, and matching it to a pricing region.

Regional pricing keys off the postcode, so two spellings of the same postcode must
price the same — `iv11hy`, `IV1 1HY` and ` iv1  1hy ` are one postcode. Everything
that stores, compares or matches a postcode goes through `normalise_postcode` first.

Matching is by prefix, the most precise row winning, with `*` as the catch-all. A
plain string prefix is right for the all-numeric systems (a Spanish province, a
French département) but wrong for the UK: the prefix `B` (Birmingham) would match
`BT1 1AA` (Belfast), and `IV1` would match `IV12 3AB`. UK rows therefore match on
the postcode's structure — area, district, sector, unit — never on characters.
"""

import re
from typing import Any, Dict, List, Optional

# The country part of a locale such as `en_GB` or `fr_BE`.
def locale_country(locale: Optional[str]) -> str:
    parts = re.split(r"[_-]", (locale or "").strip())
    return parts[1].upper() if len(parts) > 1 else ""


_GB = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)(\d[A-Z]{2})$")
_NL = re.compile(r"^(\d{4})([A-Z]{2})$")

# Countries whose postcodes are a fixed run of digits.
_DIGITS = {"ES": 5, "FR": 5, "IT": 5, "DE": 5, "BE": 4}


def normalise_postcode(value: Any, locale: Optional[str]) -> Optional[str]:
    """The canonical spelling of `value` for the locale's country, or None if it is not one.

    None covers both "nothing sent" and "not a postcode in this country" — callers that
    need to tell those apart check for an empty input themselves. A country this module
    does not know returns None: regional pricing must not guess at a format it cannot
    check, because a postcode it cannot read is one it cannot price.
    """
    if not isinstance(value, str):
        return None
    compact = re.sub(r"\s+", "", value).upper()
    if not compact:
        return None
    country = locale_country(locale)
    if country == "GB":
        match = _GB.match(compact)
        return f"{match.group(1)} {match.group(2)}" if match else None
    if country == "NL":
        match = _NL.match(compact)
        return f"{match.group(1)} {match.group(2)}" if match else None
    if country in _DIGITS:
        return compact if re.fullmatch(rf"\d{{{_DIGITS[country]}}}", compact) else None
    return None


def postcode_format_known(locale: Optional[str]) -> bool:
    """Whether `normalise_postcode` can check postcodes for the locale's country."""
    country = locale_country(locale)
    return country in ("GB", "NL") or country in _DIGITS


def address_postcode(value: Any, locale: Optional[str]) -> Optional[str]:
    """A postcode to store as an address: normalised where the format is known.

    Where it is not, the value is only tidied, so a client in another country is never
    blocked from booking a repair. Regional pricing does not use this — it needs
    `normalise_postcode`, and refuses a postcode it cannot check.
    """
    if postcode_format_known(locale):
        return normalise_postcode(value, locale)
    if not isinstance(value, str):
        return None
    tidy = re.sub(r"\s+", " ", value.strip()).upper()
    return tidy if re.fullmatch(r"[A-Z0-9][A-Z0-9 -]{1,14}", tidy) else None


def _gb_matches(prefix: str, postcode: str) -> bool:
    """Whether a UK region row covers this (already normalised) UK postcode.

    The row may name an area (`IV`), a district (`IV1`), a sector (`IV1 1`) or a
    whole postcode (`IV1 1HY`). Each is compared against the same level of the
    postcode, so `B` is not `BT` and `IV1` is not `IV12`.
    """
    outward, inward = postcode.split(" ")
    area = re.match(r"^[A-Z]+", outward).group(0)
    row = re.sub(r"\s+", " ", prefix.strip().upper())
    if re.fullmatch(r"[A-Z]{1,2}", row):
        return row == area
    if " " not in row:
        return row == outward
    row_out, row_in = row.split(" ", 1)
    return row_out == outward and inward.startswith(row_in)


def _row_matches(prefix: str, postcode: str, country: str) -> bool:
    if country == "GB":
        return _gb_matches(prefix, postcode)
    return postcode.replace(" ", "").startswith(re.sub(r"\s+", "", prefix).upper())


def find_region(regions: List[Dict[str, Any]], postcode: str, locale: Optional[str]) -> Optional[Dict[str, Any]]:
    """The region row covering `postcode`: the most precise match, else `*`, else None.

    "Most precise" is the longest prefix, ignoring spaces, so a sector beats its
    district and a district beats its area. Rows without a usable `prefix` are skipped.
    """
    country = locale_country(locale)
    best, best_len, fallback = None, -1, None
    for row in regions or []:
        if not isinstance(row, dict):
            continue
        prefix = row.get("prefix")
        if not isinstance(prefix, str) or not prefix.strip():
            continue
        if prefix.strip() == "*":
            fallback = fallback or row
            continue
        if not _row_matches(prefix, postcode, country):
            continue
        length = len(re.sub(r"\s+", "", prefix))
        if length > best_len:
            best, best_len = row, length
    return best or fallback
