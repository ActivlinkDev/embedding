# routers/email_ingest.py
# ==========================
# Purpose: Ingest order-confirmation emails via IMAP (or raw pasted email),
#          extract structured JSON via GPT, include attachments (base64),
#          and save into MongoDB collection "Receipts".
# ==========================

import os, imaplib, email, time, hashlib
from email.header import decode_header, make_header
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient

from utils.dependencies import verify_token
from utils.email_extract import (
    extract_text_and_attachments_from_email_message,
    extract_structured_fields_strict_json,
    html_to_text,
)

# ✅ FastAPI router object (required by main.py)
router = APIRouter(prefix="/email/ingest", tags=["Email Ingest"])

# ----------------------
# Environment Variables
# ----------------------
IMAP_HOST = os.getenv("IMAP_HOST")
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASS = os.getenv("IMAP_PASS")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "Activlink")
RECEIPTS_COLLECTION = os.getenv("RECEIPTS_COLLECTION", "Receipts")

# ----------------------
# Mongo connection helper
# ----------------------
_mclient: Optional[AsyncIOMotorClient] = None

def get_db():
    global _mclient
    if _mclient is None:
        _mclient = AsyncIOMotorClient(MONGO_URI)
    return _mclient[MONGO_DB]

# ----------------------
# Pydantic Models
# ----------------------
class ExtractRequest(BaseModel):
    """Input model for /parse when pasting raw email."""
    raw_email_text: str = Field(..., description="Full email text or HTML")

class ExtractResponse(BaseModel):
    """Response model for both /parse and /poll."""
    receipt_id: Optional[str] = None
    extracted: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)

# ----------------------
# Utility
# ----------------------
def _hash_text(s: str) -> str:
    """Create SHA256 hash of raw text (deduplication)."""
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

# ----------------------
# Routes
# ----------------------

@router.post("/parse", response_model=ExtractResponse, dependencies=[Depends(verify_token)])
async def parse_email(req: ExtractRequest):
    """
    Dev/test endpoint:
      1. Accept pasted email text/HTML
      2. Normalize to plaintext
      3. Run GPT extraction with strict JSON prompt
      4. Insert into Mongo Receipts collection
    """
    # 1) Normalize HTML → plain text if needed
    text = html_to_text(req.raw_email_text) if "<html" in req.raw_email_text.lower() else req.raw_email_text

    # 2) Run GPT extraction
    extracted, warns = extract_structured_fields_strict_json(text)

    # 3) Build receipt document
    receipt_doc = {
        "source": "manual_parse",
        "headers": {},
        "extracted": extracted,
        "attachments": [],  # none via paste
        "raw_text_hash": _hash_text(text),
        "created_at": int(time.time()),
        "warnings": warns[:],
    }

    # 4) Save to Mongo
    db = get_db()
    ins = await db[RECEIPTS_COLLECTION].insert_one(receipt_doc)
    receipt_id = str(ins.inserted_id)

    return ExtractResponse(
        receipt_id=receipt_id,
        extracted=extracted,
        warnings=warns
    )


@router.post("/poll", response_model=List[ExtractResponse], dependencies=[Depends(verify_token)])
async def poll_imap(limit: int = Query(10, ge=1, le=200)):
    """
    Poll IMAP inbox for unseen messages:
      1. Fetch messages
      2. Extract plain text + attachments (base64)
      3. Run GPT extraction with header hints
      4. Insert into Mongo Receipts
      5. Mark message as seen
    """
    if not all([IMAP_HOST, IMAP_USER, IMAP_PASS]):
        raise HTTPException(500, "IMAP credentials not configured")

    results: List[ExtractResponse] = []

    # Connect to IMAP
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(IMAP_USER, IMAP_PASS)
    mail.select(IMAP_FOLDER)

    # Search for UNSEEN messages
    typ, data = mail.search(None, "UNSEEN")
    if typ != "OK":
        raise HTTPException(500, "IMAP search failed")

    ids = list(reversed(data[0].split()))[:limit]
    db = get_db()

    for eid in ids:
        # 1) Fetch message
        _, msg_data = mail.fetch(eid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        # Extract headers
        hdr_from = str(make_header(decode_header(msg.get("From", ""))))
        hdr_to = str(make_header(decode_header(msg.get("To", ""))))
        hdr_subject = str(make_header(decode_header(msg.get("Subject", ""))))
        hdr_date = str(make_header(decode_header(msg.get("Date", ""))))
        hdr_msgid = str(make_header(decode_header(msg.get("Message-ID", ""))))

        # 2) Extract text + attachments
        text, attachments, warnings = extract_text_and_attachments_from_email_message(msg)

        # 3) Run GPT extraction with header hints
        extracted, warns2 = extract_structured_fields_strict_json(
            text,
            hdr_from=hdr_from,
            hdr_to=hdr_to,
            hdr_subject=hdr_subject,
            hdr_date=hdr_date
        )
        warnings.extend(warns2)

        # 4) Build and insert receipt doc
        receipt_doc = {
            "source": "imap",
            "headers": {
                "from": hdr_from,
                "to": hdr_to,
                "subject": hdr_subject,
                "date": hdr_date,
                "message_id": hdr_msgid,
            },
            "extracted": extracted,
            "attachments": attachments,
            "raw_text_hash": _hash_text(text),
            "created_at": int(time.time()),
            "warnings": warnings[:],
        }

        ins = await db[RECEIPTS_COLLECTION].insert_one(receipt_doc)
        receipt_id = str(ins.inserted_id)

        # 5) Mark email as seen
        mail.store(eid, "+FLAGS", "\\Seen")

        # Collect response
        results.append(ExtractResponse(
            receipt_id=receipt_id,
            extracted=extracted,
            warnings=warnings
        ))

    try:
        mail.logout()
    except Exception:
        pass

    return results
