"""The fixed vocabulary of fault types.

Every fault in the catalogue is classified into exactly one of these types, for every
category and every locale. The vocabulary is **owned by this module, not by the language
model**: if each category were free to invent its own types, "Won't switch on" and "No
power" would both appear, the grouping would drift between categories and locales, and
nothing downstream could group or report on a reported fault.

So generation is handed this enum and must choose from it. Anything else is coerced to
``other`` rather than stored — see :func:`normalise_type_id`.

The English labels here are the fallback shown when a catalogue entry carries no
localized label of its own. ``TYPE_ORDER`` fixes the order the types are presented in, so
the customer sees the same cards in the same places whatever the device.
"""

from typing import Dict, List

OTHER = "other"

# typeId -> English label. Order is the presentation order.
FAULT_TYPES: Dict[str, str] = {
    "no_power": "Won't turn on / no power",
    "performance": "Works, but not properly",
    "noise_vibration": "Unusual noise or vibration",
    "leak_water": "Leaking or water damage",
    "heat_smell": "Overheating or burning smell",
    "door_seal_hinge": "Door, lid, seal or hinge",
    "display_controls": "Display, controls or software",
    "connectivity": "Won't connect (Wi-Fi, app, pairing)",
    "physical_damage": "Accidental or physical damage",
    OTHER: "Something else",
}

TYPE_ORDER: List[str] = list(FAULT_TYPES)

# Position of each type, for sorting. Anything unknown sorts to the end, beside `other`.
_TYPE_RANK: Dict[str, int] = {type_id: index for index, type_id in enumerate(TYPE_ORDER)}


def is_known_type(type_id: str) -> bool:
    """Whether ``type_id`` is part of the fixed vocabulary."""
    return isinstance(type_id, str) and type_id in FAULT_TYPES


def normalise_type_id(type_id: object) -> str:
    """Coerce a generated or caller-supplied type id into the vocabulary.

    Matching is forgiving about case, spaces and hyphens, because a language model asked
    for ``no_power`` will occasionally answer ``No Power``. Anything still unrecognised
    becomes ``other``, so a bad generation degrades to an extra entry under "Something
    else" rather than inventing a type that no other category shares.
    """
    if not isinstance(type_id, str):
        return OTHER
    folded = type_id.strip().lower().replace(" ", "_").replace("-", "_")
    return folded if folded in FAULT_TYPES else OTHER


def type_label(type_id: str) -> str:
    """The English label for a type id, falling back to the ``other`` label."""
    return FAULT_TYPES.get(type_id, FAULT_TYPES[OTHER])


def type_rank(type_id: str) -> int:
    """Sort key placing types in ``TYPE_ORDER``; unknown ids sort last."""
    return _TYPE_RANK.get(type_id, len(TYPE_ORDER))
