"""Compatibility helpers for Strapi v5 REST responses.

Strapi v5 changed the REST response shape compared with v4:

* entry fields moved from a nested ``attributes`` object up to the top level
* entries gained a ``documentId``
* media and relations are returned as plain objects/arrays instead of the
  v4 ``{"data": ...}`` envelope (e.g. ``Images`` is now a list of file
  objects rather than ``{"data": [{"id", "attributes": {...}}]}``)

The Next.js frontend (and other in-process callers) were written against the
v4 shape and read values like ``item.attributes.Product_ID`` and
``Images.data[].attributes.url``. After the v4 -> v5 upgrade those reads
silently return nothing.

``normalize_strapi_response`` re-wraps a v5 payload back into the v4
structure so existing consumers keep working. It is intentionally defensive:
anything already in v4 shape (has ``attributes``) is passed through
unchanged, so it is safe to apply unconditionally.
"""

from typing import Any


def _is_media_file(value: Any) -> bool:
    """Heuristic test for a Strapi media file object.

    A media file always carries a ``url`` plus at least one file-specific
    field. Requiring one of those extra keys avoids misclassifying arbitrary
    JSON values that merely contain a ``url`` string.
    """
    return (
        isinstance(value, dict)
        and "url" in value
        and any(k in value for k in ("mime", "ext", "hash", "formats"))
    )


def _is_relation_entry(value: Any) -> bool:
    """Test for a populated v5 relation entry.

    Relation entries carry a ``documentId`` and are not yet v4-wrapped. Plain
    JSON objects (json-type fields) lack ``documentId`` and so are left alone.
    """
    return (
        isinstance(value, dict)
        and "documentId" in value
        and "attributes" not in value
    )


def _to_v4_value(value: Any) -> Any:
    """Recursively convert a single v5 field value to its v4 shape."""
    if isinstance(value, list):
        # A list made entirely of media files is a multiple-media field.
        if value and all(_is_media_file(v) for v in value):
            return {"data": [{"id": v.get("id"), "attributes": v} for v in value]}
        # A list of relation entries (identified by ``documentId``) is a
        # repeatable relation; re-envelope it like the single-relation case.
        if value and all(_is_relation_entry(v) for v in value):
            return {"data": [_entry_to_v4(v) for v in value]}
        return [_to_v4_value(v) for v in value]

    if _is_media_file(value):
        return {"data": {"id": value.get("id"), "attributes": value}}

    if _is_relation_entry(value):
        # Single relation -> v4 ``{"data": {...}}`` envelope.
        return {"data": _entry_to_v4(value)}

    return value


def _entry_to_v4(entry: dict) -> dict:
    """Wrap a flattened v5 entry's fields back under ``attributes``."""
    if "attributes" in entry:
        # Already v4 shaped (or defensively treated as such).
        return entry
    attributes = {
        key: _to_v4_value(val) for key, val in entry.items() if key != "id"
    }
    return {"id": entry.get("id"), "attributes": attributes}


def normalize_strapi_response(payload: Any) -> Any:
    """Convert a Strapi v5 REST payload into the v4-compatible shape.

    Handles both collection responses (``data`` is a list) and single-type
    responses (``data`` is an object). Non-Strapi payloads are returned
    unchanged.
    """
    if not isinstance(payload, dict) or "data" not in payload:
        return payload

    data = payload["data"]
    if isinstance(data, list):
        payload["data"] = [
            _entry_to_v4(entry) if isinstance(entry, dict) else entry
            for entry in data
        ]
    elif isinstance(data, dict):
        payload["data"] = _entry_to_v4(data)

    return payload
