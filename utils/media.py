"""Validation for base64 uploads that end up as Mongo Binary.

Two journeys accept a file as base64 JSON — a proof-of-purchase receipt during
registration, and a fault photo when a customer books a repair. Both need the same three
checks, and they need to agree: a validator that drifts between the two is a validator
where one of them quietly accepts something the other rejects.

The checks, in order:

1. **Strict base64 decode.** ``validate=True`` so padding and alphabet errors are caught
   here rather than producing silently truncated bytes.
2. **A size cap**, applied to the *decoded* bytes. The caller also caps the base64 string
   at the Pydantic layer, which bounds what has to be decoded in the first place.
3. **Magic bytes**, checked against the declared content type. This is the check that
   matters: a ``.txt`` renamed to ``.jpg`` arrives with ``contentType: image/jpeg`` and is
   only caught by looking at the bytes. Each caller passes the types it actually allows,
   so a PDF is a valid receipt and never a valid fault photo.
"""

import base64
import binascii
from datetime import datetime, timezone
from typing import Dict, Iterable, Set

from bson import Binary
from fastapi import HTTPException

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

IMAGE_TYPES: Set[str] = {"image/jpeg", "image/png", "image/heic", "image/heif"}
RECEIPT_TYPES: Set[str] = IMAGE_TYPES | {"application/pdf"}


def _signatures(data: bytes) -> Dict[str, bool]:
    """Whether ``data`` starts with each known type's magic bytes."""
    return {
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "application/pdf": data.startswith(b"%PDF-"),
        "image/heic": data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"hevx"),
        "image/heif": data[4:8] == b"ftyp" and data[8:12] in (b"mif1", b"msf1"),
    }


def decode_upload(
    name: str,
    content_type: str,
    data_b64: str,
    allowed: Iterable[str],
    *,
    invalid_message: str = "Invalid file",
    too_large_message: str = "File must be 5 MB or smaller.",
    wrong_type_message: str = "Unsupported file type.",
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> dict:
    """Decode and validate one upload, returning the document to store.

    Raises `HTTPException` 400 (undecodable), 413 (empty or oversized) or 415 (the bytes
    are not what the content type claims, or the type is not in ``allowed``).
    """
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(400, invalid_message)
    if not data or len(data) > max_bytes:
        raise HTTPException(413, too_large_message)
    allowed = set(allowed)
    if content_type not in allowed or not _signatures(data).get(content_type):
        raise HTTPException(415, wrong_type_message)
    return {
        "name": name,
        "contentType": content_type,
        "size": len(data),
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
        "data": Binary(data),
    }
