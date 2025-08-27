# routers/email_ingest.py
"""
Email ingest router:
- Reads mailbox configs from mailboxes.json
- Pulls recent emails via IMAP
- Extracts text + attachments
- Uses LLM to produce structured JSON (incl. Customer Phone in E.164)
- Writes a receipt doc to MongoDB ("Receipts" collection)

Environment:
- MONGODB_URI (mongodb+srv://...)
- MONGODB_DB (default: activlink)
"""

from __future__ import annotations

import email
import imaplib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorClient

from utils.email_extract import (
    extract_structured_fields_strict_json,
    extract_text_and_attachments_from_email_message,
)

router = APIRouter(prefix="/email/ingest", tags=["email-ingest"])

# ────────────────────────────────────────────────────────────────────────────────
# Mongo
# ────────────────────────────────────────────────────────────────────────────────
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "activlink")

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is required")

_mongo_client = AsyncIOMotorClient(MONGODB_URI)
_db = _mongo_client[MONGODB_DB]
_receipts = _db["Receipts"]


# ────────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────────
def _load_mailboxes(path: str = "mailboxes.json") -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} not found")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("mailboxes.json must be a list of mailbox configs")
    return data


def _imap_fetch_recent(
    host: str, user: str, password: str, folder: str, limit: int
) -> List[Tuple[bytes, bytes]]:
    """
    Returns list of (msg_id, raw_bytes) for the most recent 'limit' emails in folder.
    """
    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        conn.select(folder, readonly=True)
        typ, data = conn.search(None, "ALL")
        if typ != "OK":
            return []
        ids = data[0].split()
        if not ids:
            return []
        # Take the last N
        ids = ids[-limit:]

        out = []
        for mid in ids:
            typ, msg_data = conn.fetch(mid, "(RFC822)")
            if typ == "OK" and msg_data:
                # msg_data can contain multiple parts, find the RFC822 bytes
                for part in msg_data:
                    if isinstance(part, tuple) and len(part) == 2:
                        out.append((mid, part[1]))
                        break
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────────
@router.post("/poll")
async def poll_mailboxes(limit: int = Query(10, ge=1, le=50)) -> Dict[str, Any]:
    """
    Pull newest 'limit' emails per mailbox, extract, and persist receipt docs.
    """
    mailboxes = _load_mailboxes()

    results: List[Dict[str, Any]] = []
    for m in mailboxes:
        mb_id = m.get("id") or "default"
        host = m.get("host")
        user = m.get("user")
        pw = m.get("pass")
        folder = m.get("folder", "INBOX")
        client_key = m.get("ClientKey")

        if not host or not user or not pw:
            # Skip misconfigured mailbox entry
            continue

        fetched = _imap_fetch_recent(host, user, pw, folder, limit)

        for msg_id, raw_bytes in fetched:
            try:
                msg = email.message_from_bytes(raw_bytes)
                headers = {
                    "From": msg.get("From", ""),
                    "To": msg.get("To", ""),
                    "Subject": msg.get("Subject", ""),
                    "Date": msg.get("Date", ""),
                    "Message-ID": msg.get("Message-ID", ""),
                    "Return-Path": msg.get("Return-Path", ""),
                }

                email_text, attachments, warn_a = extract_text_and_attachments_from_email_message(msg)
                extracted, warn_b = extract_structured_fields_strict_json(headers, email_text)

                warnings = (warn_a or []) + (warn_b or [])

                # "extracted" now includes "Customer Phone" in E.164 when present
                receipt_doc = {
                    "mailbox_id": mb_id,
                    "ClientKey": client_key,
                    "ingested_at": datetime.now(timezone.utc).isoformat(),
                    "headers": headers,
                    "text": email_text,
                    "attachments": [
                        {
                            "filename": a.get("filename"),
                            "content_type": a.get("content_type"),
                            "size": a.get("size"),
                            "data_base64": a.get("data_base64"),
                        }
                        for a in attachments
                    ],
                    "extracted": extracted,
                    "warnings": warnings or None,
                    "raw_message_id": msg_id.decode("ascii", "ignore"),
                }

                insert_res = await _receipts.insert_one(receipt_doc)
                results.append(
                    {
                        "mailbox_id": mb_id,
                        "message_id": headers.get("Message-ID"),
                        "receipt_id": str(insert_res.inserted_id),
                        "subject": headers.get("Subject"),
                        "customer_phone": (extracted or {}).get("Customer Phone"),
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "mailbox_id": mb_id,
                        "error": str(e),
                    }
                )

    return {"ok": True, "count": len(results), "results": results}
