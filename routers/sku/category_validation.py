"""Reject a caller-supplied category that is not in the `Category` taxonomy.

A category supplied on a SKU request — `Category` on a create, `Category` or
`Locale_Details.Category` on a CustomSKU override — was previously stored
verbatim when it did not match a taxonomy entry. That produces a SKU that
cannot be rated (nothing matches at category level, and a free-typed name has
no group or sector for a broader rule to catch) and cannot be translated (no
`Category` document means no `locale_title`, so every locale renders the raw
string). Both failures are silent and only surface as a wrong or missing price.

Failing the request is the safe direction: the caller finds out at SKU creation
rather than a customer finding out at quote time.

Categories derived from enrichment data are not validated here — those go
through the embedding fallback in `create_master_sku._canonical_category`,
which already resolves to a taxonomy entry or to nothing.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException

from utils.category_tree import known, loaded, resolve


logger = logging.getLogger(__name__)


def validate_category(value: Optional[str], field: str = "Category") -> Optional[str]:
    """Return `value` in the taxonomy's own spelling, or raise 422.

    `None` and blank pass straight through: an explicit null override means
    "suppress the inherited value", which is a different intent from naming a
    category, and blanks are rejected by the callers that care.

    A taxonomy that cannot be read is not treated as a taxonomy that rejects
    everything — a Mongo blip must not block all SKU creation — so the caller's
    own spelling is kept and the fact is logged.
    """
    if value is None:
        return None
    name = value.strip()
    if not name:
        return value
    if known(name):
        # The taxonomy's spelling, so the stored value compares equal to rating
        # and assignment rule lists even when the caller wrote "washer-dryer".
        return resolve(name)["category"] or name
    if not loaded():
        logger.warning(
            "[category] accepting unvalidated %s=%r: the Category taxonomy is unreadable",
            field, name,
        )
        return name
    raise HTTPException(
        status_code=422,
        detail=(
            f"{field} '{name}' is not in the Category taxonomy. Rating and assignment "
            f"match on it, so it has to be an existing category. Use POST /match to find "
            f"the closest one, or omit {field} to have it derived from the product data."
        ),
    )
