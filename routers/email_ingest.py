# routers/email_ingest.py

import os, imaplib, email, time, hashlib, json
from email.header import decode_header, make_header
from email.utils import getaddresses
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient

from utils.dependencies import verify_token

# ✅ FastAPI router object
router = APIRouter(prefix="/email/ingest", tags=["Email Ingest"])

# ----------------------
# Load mailbox configs
# ----------------------
MAILBOXES: List[Dict[str, Any]] = []
if os.getenv("MAILBOXES_JSON"):
    try:
        MAILBOXES = json.loads(os.getenv("MAILBOXES_JSON"))
        print(f"[EMAIL-INGEST] Loaded {len(MAILBOXES)} mailbox(es) from MAILBOXES_JSON")
    except Exception as e:
        print(f"[EMAIL-INGEST] Failed to parse MAILBOXES_JSON: {e}")
else:
    path = os.getenv("MAILBOXES_PATH", "mailboxes.json")
    try:
        with open(path, "r") as f:
            MAILBOXES = json.load(f)
        print(f"[EMAIL-INGEST] Loaded {len(MAILBOXES)} mailbox(es) from {path}")
    except Exception as e:
        print(f"[EMAIL-INGEST] No mailboxes.json file and MAILBOXES_JSON not set: {e}")
        MAILBOXES = []

# ----------------------
# Mongo connection helper
# ----------------------
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "Activlink")
RECEIPTS_COLLECTION = os.getenv("RECEIPTS_COLLECTION", "Receipts")

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
    raw_email_text: str = Field(..., description="Full email text or HTML")

class ExtractResponse(BaseModel):
    receipt_id: Optional[str] = None
    extracted: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)

# ----------------------
# Utilities
# ----------------------
def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def _first_valid_address(addr_headers: List[str]) -> Optional[str]:
    addr_headers = [h for h in addr_headers if h]
    if not addr_headers:
        return None
    parsed = getaddresses(addr_headers)
    for _, email_addr in parsed:
        if email_addr and "@" in email_addr:
            return email_addr.strip().lower()
    return None

# ----------------------
# Poll a single mailbox (LAZY imports to avoid circulars)
# ----------------------
async def poll_mailbox(config: dict, limit: int = 10) -> List[ExtractResponse]:
    from utils import email_extract as EE  # lazy import

    results: List[ExtractResponse] = []
    mailbox_id = config["id"]

    mail = imaplib.IMAP4_SSL(config["host"])
    mail.login(config["user"], config["pass"])
    mail.select(config.get("folder", "INBOX"))

    typ, data = mail.search(None, "UNSEEN")
    if typ != "OK":
        raise HTTPException(500, f"IMAP search failed for {mailbox_id}")

    ids = list(reversed(data[0].split()))[:limit]
    db = get_db()

    for eid in ids:
        _, msg_data = mail.fetch(eid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        # ---- Extract headers ----
        hdr_from = str(make_header(decode_header(msg.get("From", ""))))
        hdr_to = str(make_header(decode_header(msg.get("To", ""))))
        hdr_subject = str(make_header(decode_header(msg.get("Subject", ""))))
        hdr_date = str(make_header(decode_header(msg.get("Date", ""))))
        hdr_msgid = str(make_header(decode_header(msg.get("Message-ID", ""))))

        delivered_to = msg.get_all("Delivered-To", []) or []
        x_original_to = msg.get_all("X-Original-To", []) or []
        envelope_to = msg.get_all("Envelope-To", []) or []
        resent_to = msg.get_all("Resent-To", []) or []
        to_list = msg.get_all("To", []) or []
        header_recipient = _first_valid_address(to_list + delivered_to + x_original_to + envelope_to + resent_to)

        # ---- Body & attachments ----
        text, attachments, warnings = EE.extract_text_and_attachments_from_email_message(msg)

        # ---- LLM extraction ----
        extracted, warns2 = EE.extract_structured_fields_strict_json(
            text, hdr_from=hdr_from, hdr_to=hdr_to, hdr_subject=hdr_subject, hdr_date=hdr_date
        )
        warnings.extend(warns2)

        # ---- Persist ----
        receipt_doc = {
            "mailbox_id": mailbox_id,
            "client_key": config.get("ClientKey"),
            "source": "imap",
            "headers": {
                "from": hdr_from,
                "to": hdr_to,
                "subject": hdr_subject,
                "date": hdr_date,
                "message_id": hdr_msgid,
                "recipient_email": header_recipient,  # stored for reference only
            },
            "extracted": extracted,
            "attachments": attachments,
            "raw_text_hash": _hash_text(text),
            "created_at": int(time.time()),
            "warnings": warnings[:],
        }

        ins = await db[RECEIPTS_COLLECTION].insert_one(receipt_doc)
        receipt_id = str(ins.inserted_id)
        mail.store(eid, "+FLAGS", "\\Seen")

        results.append(ExtractResponse(receipt_id=receipt_id, extracted=extracted, warnings=warnings))

    try:
        mail.logout()
    except Exception:
        pass

    return results

# ----------------------
# Routes
# ----------------------
@router.post("/parse", response_model=ExtractResponse, dependencies=[Depends(verify_token)])
async def parse_email(req: ExtractRequest):
    from utils import email_extract as EE
    text = EE.html_to_text(req.raw_email_text) if "<html" in req.raw_email_text.lower() else req.raw_email_text
    extracted, warns = EE.extract_structured_fields_strict_json(text)

    receipt_doc = {
        "source": "manual_parse",
        "headers": {},
        "extracted": extracted,
        "attachments": [],
        "raw_text_hash": _hash_text(text),
        "created_at": int(time.time()),
        "warnings": warns[:],
    }

    db = get_db()
    ins = await db[RECEIPTS_COLLECTION].insert_one(receipt_doc)
    receipt_id = str(ins.inserted_id)

    return ExtractResponse(receipt_id=receipt_id, extracted=extracted, warnings=warns)

@router.post("/poll", response_model=List[ExtractResponse], dependencies=[Depends(verify_token)])
async def poll(id: str, limit: int = Query(10, ge=1, le=200)):
    config = next((c for c in MAILBOXES if c["id"] == id), None)
    if not config:
        raise HTTPException(404, f"No mailbox config found for id={id}")
    return await poll_mailbox(config, limit)
