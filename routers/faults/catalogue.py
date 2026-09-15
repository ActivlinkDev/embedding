"""The grouped fault catalogue a customer picks from when reporting a problem.

`routers/generate_faults.py` returns one flat list of five issues per category and locale.
That is the wrong shape for someone choosing a fault on a phone, and the entries carry no
classification, so nothing can group them. This module serves the same idea grouped by the
fixed vocabulary in `fault_types.py`: the customer taps a fault *type* first, then picks a
specific fault from the handful behind it.

Entries are cached per category and locale in the `FaultCatalogue` collection and generated
on first use, exactly as `generate_faults` does. Two differences matter:

* A generation failure returns an **empty catalogue with 200**, not a 500. The repair
  journey degrades to a free-text description, and an OpenAI outage must not stop a
  customer booking a repair.
* Entries marked ``source: "curated"`` are never regenerated, so hand-written copy
  survives a re-run of the seeding script.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import openai
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from utils.api_docs import json_response, secured
from utils.dependencies import verify_token
from utils.mongo import require_client

from .fault_types import (
    FAULT_TYPES,
    OTHER,
    TYPE_ORDER,
    normalise_type_id,
    type_label,
    type_rank,
)

router = APIRouter(prefix="/faults", tags=["Catalog"])

_client = require_client()
_db = _client["Activlink"]
catalogue_collection = _db["FaultCatalogue"]



def _openai() -> "openai.OpenAI":
    """The OpenAI client, built on first use.

    Deliberately not built at import time: `main._include_router` swallows import errors,
    so a module-level client would make this whole router vanish from the API whenever
    OPENAI_API_KEY is unset, with nothing but a startup log to say so. Built lazily, a
    missing key surfaces as the designed fallback instead — an empty catalogue and a
    free-text description.
    """
    return openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# How many faults to ask for per category. Enough to cover a type-grouped list without
# giving any single card more entries than fits a phone screen.
FAULTS_PER_CATEGORY = 14

SOURCE_LLM = "llm"
SOURCE_CURATED = "curated"


class CatalogueRequest(BaseModel):
    """Which category and locale to serve the fault catalogue for."""

    clientkey: str = Field(
        ...,
        min_length=1,
        description="**Mandatory.** The tenant asking. Scoped by `verify_token`.",
        examples=["AOPON12345"],
    )
    category: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="**Mandatory.** Device category, matched **exactly** against the stored `Category`.",
        examples=["Dishwasher"],
    )
    locale: str = Field(
        ...,
        pattern=r"^[a-z]{2}_[A-Z]{2}$",
        description="**Mandatory.** Locale the catalogue should be written in.",
        examples=["en_GB"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {"clientkey": "AOPON12345", "category": "Dishwasher", "locale": "en_GB"}
        }
    }


def ensure_fault_catalogue_indexes() -> None:
    """Indexes for the catalogue. Safe to call repeatedly."""
    catalogue_collection.create_index("Category", unique=True)
    catalogue_collection.create_index([("Category", 1), ("Content.locale", 1)])


def find_locale_content(doc: Optional[dict], locale: str) -> Optional[dict]:
    """The `Content` entry for ``locale``, or None."""
    for content in (doc or {}).get("Content", []) or []:
        if content.get("locale") == locale:
            return content
    return None


def group_faults(faults: List[Dict[str, Any]], locale_labels: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Group flat faults into the fixed type vocabulary, in ``TYPE_ORDER``.

    Types with no faults are dropped rather than returned empty, so the customer is never
    shown a card with nothing behind it — a kettle has no connectivity faults.
    """
    labels = locale_labels or {}
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for fault in faults:
        type_id = normalise_type_id(fault.get("typeId"))
        buckets.setdefault(type_id, []).append(
            {
                "faultId": fault.get("faultId") or "",
                "Issue": fault.get("Issue") or "",
                "Description": fault.get("Description") or "",
                "Solution": fault.get("Solution") or "",
            }
        )
    return [
        {
            "typeId": type_id,
            "typeLabel": labels.get(type_id) or type_label(type_id),
            "faults": buckets[type_id],
        }
        for type_id in sorted(buckets, key=type_rank)
        if buckets[type_id]
    ]


def _assign_fault_ids(faults: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Give every fault a stable id of the form ``<typeId>-NN`` within this catalogue."""
    counters: Dict[str, int] = {}
    assigned = []
    for fault in faults:
        type_id = normalise_type_id(fault.get("typeId"))
        counters[type_id] = counters.get(type_id, 0) + 1
        assigned.append({**fault, "typeId": type_id, "faultId": f"{type_id}-{counters[type_id]:02d}"})
    return assigned


def _build_prompt(category: str, locale: str) -> str:
    vocabulary = "\n".join(f"- {type_id}: {label}" for type_id, label in FAULT_TYPES.items())
    return (
        f"List the {FAULTS_PER_CATEGORY} most common faults a customer would report for this device: {category}.\n"
        "Include at least one example of accidental damage.\n"
        "Classify every fault into exactly one of these fault types, using the id on the left:\n"
        f"{vocabulary}\n"
        f"Only use a type that genuinely applies to a {category}; skip types that do not.\n"
        f"Write Issue, Description and Solution in the language of locale {locale}, and also "
        f"translate the label for each type you use into that language as typeLabel.\n"
        "Issue is a short customer-facing phrase (under 8 words). Description is around 15 words "
        "describing the symptom. Solution is how a repairer typically fixes it, naming the part "
        "where possible.\n"
        'Respond with JSON only, in this shape: '
        '{"faults": [{"typeId": "...", "typeLabel": "...", "Issue": "...", "Description": "...", "Solution": "..."}]}'
    )


def _parse_model_json(content_text: str) -> Any:
    """Parse the model's reply, tolerating a ```json fenced block."""
    text = (content_text or "").strip()
    if text.startswith("```"):
        text = text[7:] if text.startswith("```json") else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


def generate_catalogue(category: str, locale: str, model: str = "gpt-4o") -> tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Generate the fault list for a category and locale.

    Returns the faults (each already carrying a vocabulary ``typeId`` and a stable
    ``faultId``) and the localized type labels the model supplied. Raises on a failure to
    call or parse; callers decide what an empty catalogue means.
    """
    response = _openai().chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": _build_prompt(category, locale)}],
        temperature=0.7,
    )
    output = _parse_model_json(response.choices[0].message.content)
    raw = output["faults"] if isinstance(output, dict) else output
    if not isinstance(raw, list) or not raw:
        raise ValueError("model returned no faults")

    faults: List[Dict[str, Any]] = []
    labels: Dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        type_id = normalise_type_id(entry.get("typeId"))
        # A localized label is only trusted for a type the model actually classified into,
        # and never for `other`, whose label is ours.
        label = entry.get("typeLabel")
        if type_id != OTHER and isinstance(label, str) and label.strip() and type_id not in labels:
            labels[type_id] = label.strip()[:120]
        faults.append(
            {
                "typeId": type_id,
                "Issue": str(entry.get("Issue") or "").strip()[:200],
                "Description": str(entry.get("Description") or "").strip()[:500],
                "Solution": str(entry.get("Solution") or "").strip()[:500],
            }
        )
    faults = [fault for fault in faults if fault["Issue"]]
    if not faults:
        raise ValueError("model returned no usable faults")
    return _assign_fault_ids(faults), labels


def store_catalogue(
    category: str,
    locale: str,
    faults: List[Dict[str, Any]],
    labels: Dict[str, str],
    source: str = SOURCE_LLM,
) -> None:
    """Upsert one locale's catalogue for a category, replacing any existing entry."""
    now = datetime.now(timezone.utc)
    content = {
        "locale": locale,
        "FaultTypes": group_faults(faults, labels),
        "source": source,
        "generatedAt": now,
    }
    # Drop any existing entry for this locale first, so a regeneration replaces rather
    # than appends — two entries for one locale would make `find_locale_content` return
    # whichever happened to be first.
    catalogue_collection.update_one(
        {"Category": category},
        {"$pull": {"Content": {"locale": locale}}, "$setOnInsert": {"Category": category}},
        upsert=True,
    )
    catalogue_collection.update_one(
        {"Category": category},
        {"$push": {"Content": content}, "$set": {"updatedAt": now}},
    )


def load_catalogue(category: str, locale: str) -> tuple[List[Dict[str, Any]], str]:
    """The stored catalogue for a category and locale, generating it on first use.

    Returns the fault-type groups and a message saying what happened. Never raises: a
    generation failure yields an empty list, and the caller renders a free-text fallback.
    """
    doc = catalogue_collection.find_one({"Category": category})
    content = find_locale_content(doc, locale)
    if content:
        return content.get("FaultTypes") or [], "Catalogue already stored for this category and locale"

    try:
        faults, labels = generate_catalogue(category, locale)
    except Exception as exc:  # noqa: BLE001 — an outage must not break the journey
        print(f"[FAULTS] catalogue generation failed for {category}/{locale}: {exc}")
        return [], "Catalogue unavailable; describe the fault in your own words"

    store_catalogue(category, locale, faults, labels)
    return group_faults(faults, labels), f"Catalogue generated for '{category}' in '{locale}'"


@router.post(
    "/catalogue",
    summary="Get (or generate) the localized fault catalogue for a category, grouped by fault type",
    response_description="The fault types that apply to this category, each with the faults behind it.",
    responses=secured({
        200: json_response(
            "The catalogue for that category and locale. `faultTypes` is empty when no catalogue "
            "could be produced — report the fault as free text in that case.",
            {
                "category": "Dishwasher",
                "locale": "en_GB",
                "message": "Catalogue already stored for this category and locale",
                "faultTypes": [
                    {
                        "typeId": "leak_water",
                        "typeLabel": "Leaking or water damage",
                        "faults": [
                            {
                                "faultId": "leak_water-01",
                                "Issue": "Water leaking from the door seal",
                                "Description": "Water escapes around the door during a wash cycle.",
                                "Solution": "Replace the perished door gasket and check the latch alignment.",
                            }
                        ],
                    }
                ],
            },
        ),
    }),
)
def fault_catalogue(req: CatalogueRequest, _: None = Depends(verify_token)):
    """
    The faults a customer can choose from for a device category, grouped into fault types.

    **Grouped, not a flat list.** Every fault carries one `typeId` from a fixed vocabulary
    shared by every category, so the same type always means the same thing and always
    carries the same label position. Types with no faults for this category are omitted,
    so nothing renders an empty card.

    **Cached, generated on first use.** The first call for a new category and locale is
    slow; every later call is served from the `FaultCatalogue` collection. Run
    `scripts/seed_fault_catalogue.py` to warm it for every category ahead of time.

    **Never fails the caller.** An unknown category or a generation failure returns `200`
    with an empty `faultTypes`, because the customer can still describe the fault in their
    own words and a repair booking must not be blocked by a missing list.
    """
    fault_types, message = load_catalogue(req.category.strip(), req.locale)
    return {
        "category": req.category.strip(),
        "locale": req.locale,
        "message": message,
        "faultTypes": fault_types,
    }
