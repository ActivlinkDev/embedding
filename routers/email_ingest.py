# routers/email_ingest.py

import os, imaplib, email, time, hashlib, json
from email.header import decode_header, make_header
from email.utils import getaddresses
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

# ✅ FastAPI router object
router = APIRouter(prefix="/email/ingest", tags=["Messaging"])

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
    """A receipt email to parse."""

    raw_email_text: str = Field(
        ...,
        description=(
            "**Mandatory.** The email body, plain text or HTML. HTML is detected automatically "
            "and converted to text before extraction."
        ),
        examples=["Order ORD-2026-00918\nBosch SMS6ZCI00G Dishwasher — £449.99\nPurchased 01/05/2025"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "raw_email_text": "Order ORD-2026-00918\nBosch SMS6ZCI00G Dishwasher — £449.99\nPurchased 01/05/2025"
            }
        }
    }

class ExtractResponse(BaseModel):
    """What was extracted from one email, and what the extractor was unsure about."""

    receipt_id: Optional[str] = Field(
        None,
        description="Id of the stored receipt document.",
        examples=["68e1f2a3b4c5d6e7f8090011"],
    )
    extracted: Dict[str, Any] = Field(
        ...,
        description=(
            "The structured fields the model found — retailer, order reference, purchase date, "
            "totals and line items. **The key set is not fixed**: it depends on what the email "
            "contained, so treat every field as optional and check before use."
        ),
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="Fields that were guessed, missing or ambiguous. A non-empty list is worth reviewing before trusting the data.",
        examples=[["purchase_date inferred from email header"]],
    )

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
@router.post(
    "/parse",
    response_model=ExtractResponse,
    dependencies=[Depends(verify_token)],
    summary="Extract purchase details from a receipt email",
    response_description="The structured fields extracted, the stored receipt id, and any warnings.",
    responses=secured({
        200: json_response("The email was parsed and stored.", {
                "receipt_id": "68e1f2a3b4c5d6e7f8090011",
                "extracted": {
                    "retailer": "Acme Retail UK",
                    "order_reference": "ORD-2026-00918",
                    "purchase_date": "2025-05-01",
                    "currency": "GBP",
                    "total": 449.99,
                    "items": [
                        {"make": "Bosch", "model": "SMS6ZCI00G", "description": "Series 6 Dishwasher", "price": 449.99}
                    ],
                    "customer_email": "jane.okafor@example.com",
                },
                "warnings": ["purchase_date inferred from email header"],
            }),
    }),
)
async def parse_email(req: ExtractRequest):
    """
    Parse a receipt email and pull out the purchase details — retailer, order reference, date,
    totals and line items.

    Paste the email in as `raw_email_text`, plain text or HTML; HTML is converted to text first.
    Extraction is done by an LLM, so **treat the result as a best effort**: the `extracted` key
    set varies with the email, and `warnings` lists anything guessed or missing. Check `warnings`
    before feeding the output into registration.

    Every parse is **stored** as a receipt document and its id returned — there is no dry-run
    mode. Nothing is registered as a device here; that is a separate step using the extracted
    fields.

    Use this for a one-off email you already hold. For mailboxes the platform monitors, use
    `POST /email/ingest/poll`.
    """
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

@router.post(
    "/poll",
    response_model=List[ExtractResponse],
    dependencies=[Depends(verify_token)],
    summary="Poll a configured mailbox and parse unread receipts",
    response_description="One entry per email processed, in the order read.",
    responses=secured({
        200: json_response(
            "The mailbox was polled. An empty array means there was nothing unread.",
            [{
                "receipt_id": "68e1f2a3b4c5d6e7f8090011",
                "extracted": {
                    "retailer": "Acme Retail UK",
                    "order_reference": "ORD-2026-00918",
                    "purchase_date": "2025-05-01",
                    "currency": "GBP",
                    "total": 449.99,
                    "items": [
                        {"make": "Bosch", "model": "SMS6ZCI00G", "description": "Series 6 Dishwasher", "price": 449.99}
                    ],
                    "customer_email": "jane.okafor@example.com",
                },
                "warnings": ["purchase_date inferred from email header"],
            }],
        ),
        404: error("No mailbox is configured with this id.", "No mailbox config found for id=acme-receipts"),
    }),
)
async def poll(
    id: str = Query(
        ...,
        description="**Mandatory.** Id of a configured mailbox, from `mailboxes.json` or `MAILBOXES_JSON`.",
        examples=["acme-receipts"],
    ),
    limit: int = Query(10, ge=1, le=200, description="Maximum unread emails to process in this run. Between 1 and 200.", examples=[10]),
):
    """
    Connect to a configured mailbox over IMAP, parse its **unread** receipt emails, and store
    what was extracted.

    `id` is mandatory and must match a mailbox in the deployment's configuration; an unknown id
    returns `404`. `limit` caps how many emails one run handles.

    **Processed emails are marked as read**, so a second call does not reprocess them — which
    also means a failed downstream step cannot be recovered by simply polling again. Each email
    is stored as a receipt with its headers, attachments and extracted fields; the response
    carries one entry per email, each with its own `warnings`.

    This is the same work the background poller does when `ENABLE_EMAIL_POLL` is on; call it
    directly to force a run or when the poller is disabled.
    """
    config = next((c for c in MAILBOXES if c["id"] == id), None)
    if not config:
        raise HTTPException(404, f"No mailbox config found for id={id}")
    return await poll_mailbox(config, limit)
