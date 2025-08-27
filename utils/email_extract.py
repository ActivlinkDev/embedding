# utils/email_extract.py
"""
Helpers for email ingestion:
- html_to_text: converts HTML emails to plain text
- extract_text_and_attachments_from_email_message: gets text + attachments
- extract_structured_fields_strict_json: LLM JSON extraction (+ Locale, Customer Phone)
"""

from __future__ import annotations

import base64
import email
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from openai import OpenAI

import phonenumbers
from phonenumbers import PhoneNumberFormat, NumberParseException

# ────────────────────────────────────────────────────────────────────────────────
# OpenAI client (uses OPENAI_API_KEY from environment)
# ────────────────────────────────────────────────────────────────────────────────
client = OpenAI()

# ────────────────────────────────────────────────────────────────────────────────
# HTML → Text
# ────────────────────────────────────────────────────────────────────────────────
def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Remove script/style
    for t in soup(["script", "style"]):
        t.extract()
    text = soup.get_text(separator="\n")
    return re.sub(r"[ \t]+\n", "\n", text).strip()


# ────────────────────────────────────────────────────────────────────────────────
# MIME parse: text + attachments (base64)
# ────────────────────────────────────────────────────────────────────────────────
def extract_text_and_attachments_from_email_message(
    msg: email.message.Message,
) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """
    Returns (text, attachments, warnings)
    attachments = [{"filename","content_type","data_base64","size"}...]
    """
    warnings: List[str] = []
    text_plain_parts: List[str] = []
    text_html_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()

            if "attachment" in disp:
                try:
                    payload = part.get_payload(decode=True) or b""
                    b64 = base64.b64encode(payload).decode("utf-8")
                    attachments.append(
                        {
                            "filename": part.get_filename(),
                            "content_type": ctype,
                            "data_base64": b64,
                            "size": len(payload),
                        }
                    )
                except Exception as e:
                    warnings.append(f"Attachment decode failed: {e}")
            elif ctype in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        payload = (part.get_payload() or "").encode("utf-8", "ignore")
                    decoded = payload.decode(part.get_content_charset() or "utf-8", "ignore")
                    if ctype == "text/plain":
                        text_plain_parts.append(decoded)
                    else:
                        text_html_parts.append(decoded)
                except Exception as e:
                    warnings.append(f"Body decode failed: {e}")
    else:
        try:
            ctype = (msg.get_content_type() or "").lower()
            payload = msg.get_payload(decode=True)
            if payload is None:
                payload = (msg.get_payload() or "").encode("utf-8", "ignore")
            decoded = payload.decode(msg.get_content_charset() or "utf-8", "ignore")
            if ctype == "text/html":
                text_html_parts.append(decoded)
            else:
                text_plain_parts.append(decoded)
        except Exception as e:
            warnings.append(f"Singlepart decode failed: {e}")

    text = "\n\n".join(text_plain_parts).strip()
    if not text and text_html_parts:
        text = html_to_text("\n\n".join(text_html_parts))

    # Compact whitespace a bit
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, attachments, warnings


# ────────────────────────────────────────────────────────────────────────────────
# LLM Extraction
# ────────────────────────────────────────────────────────────────────────────────
STRICT_PROMPT_TEMPLATE = """Extract the following details from the order confirmation email and return strict JSON.

Top-level fields:
Customer Name
Customer Email
Customer Phone
Customer Address -> object with:
  Street
  City
  Postal Code
  Region
  Country
Order Number
Purchase Date (ISO 8601: YYYY-MM-DDTHH:MM:SSZ)
Payment Method (include card type + last 4 digits if available)
Retailer Name
Locale
  - Use the email's language (detected from the body) and the Customer Address Country
  - Return in ll_CC format where 'll' is ISO 639-1 language (lowercase) and 'CC' is ISO 3166-1 alpha-2 country (UPPERCASE)
  - Examples: English + United Kingdom -> "en_GB"; French + France -> "fr_FR"; Spanish + Mexico -> "es_MX"
  - If either cannot be inferred, set Locale to null

For each purchased item, return Items[] with:
Make
Model
Purchase Price -> {{ "Amount": decimal, "Currency": 3-char ISO }}
GTIN -> if found else null
RetailerReference -> retailer product code/identifier (e.g., ASIN for Amazon) if found else null

Rules:
- If some information is missing, set it to null.
- Do not include any extra keys not requested.
- Prefer the Customer Address (not the retailer’s address).
- Use the header hints when helpful to resolve ambiguities.

Header hints:
From: {hdr_from}
To: {hdr_to}
Subject: {hdr_subject}
Date: {hdr_date}

Email text:
{email_text}
"""


def _extract_json_block(text: str) -> str:
    """
    Attempt to pull the first JSON object from a model response.
    """
    # Quick fence search
    fence = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()

    # Fallback: first {...} blob
    obj = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if obj:
        return obj.group(0).strip()

    return "{}"


# ────────────────────────────────────────────────────────────────────────────────
# Normalizers / Helpers
# ────────────────────────────────────────────────────────────────────────────────
def _normalize_purchase_prices(data: Dict[str, Any], warnings: List[str]) -> None:
    try:
        items = data.get("Items")
        if not isinstance(items, list):
            return
        for it in items:
            pp = it.get("Purchase Price")
            if isinstance(pp, dict):
                # Force keys + types
                amt = pp.get("Amount")
                cur = pp.get("Currency")
                try:
                    if amt is not None:
                        amt = float(amt)
                except Exception:
                    amt = None
                if isinstance(cur, str):
                    cur = cur.strip().upper()[:3] if len(cur) >= 3 else None
                else:
                    cur = None
                it["Purchase Price"] = {"Amount": amt, "Currency": cur}
            else:
                it["Purchase Price"] = {"Amount": None, "Currency": None}
    except Exception as e:
        warnings.append(f"Price normalization error: {e}")


def _normalize_locale(data: Dict[str, Any], warnings: List[str]) -> None:
    """
    Ensure Locale is ll_CC (e.g., en_GB). If not available, try to derive from Country.
    """
    try:
        loc = data.get("Locale")
        if isinstance(loc, str) and len(loc) == 5 and loc[2] == "_":
            ll, cc = loc.split("_", 1)
            ll = ll.lower()
            cc = cc.upper()
            data["Locale"] = f"{ll}_{cc}"
            return

        # Derive from Country if present (assume English)
        addr = data.get("Customer Address") or {}
        country = (addr.get("Country") or "").strip()
        if country:
            cc = _country_name_to_cc(country)
            data["Locale"] = f"en_{cc}" if cc else None
        else:
            data["Locale"] = None
    except Exception as e:
        warnings.append(f"Locale normalization error: {e}")
        data["Locale"] = None


_COUNTRY_HINTS = {
    # Extend as needed
    "uk": "GB",
    "u.k.": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "england": "GB",
    "wales": "GB",
    "scotland": "GB",
    "northern ireland": "GB",
    "ireland": "IE",
    "republic of ireland": "IE",
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
    "france": "FR",
    "germany": "DE",
    "spain": "ES",
    "italy": "IT",
    "portugal": "PT",
    "netherlands": "NL",
    "belgium": "BE",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "austria": "AT",
    "switzerland": "CH",
    "poland": "PL",
    "mexico": "MX",
    "canada": "CA",
    "australia": "AU",
    "new zealand": "NZ",
    "croatia": "HR",
    "montenegro": "ME",
}


def _country_name_to_cc(country_name: str) -> Optional[str]:
    key = country_name.strip().lower()
    return _COUNTRY_HINTS.get(key)


def _infer_region_from_data(data: dict) -> Optional[str]:
    # Try Locale first (ll_CC)
    loc = data.get("Locale")
    if isinstance(loc, str) and len(loc) == 5 and loc[2] == "_":
        return loc.split("_", 1)[1]

    # Then try the Customer Address Country
    try:
        addr = data.get("Customer Address") or {}
        country = (addr.get("Country") or "").strip().lower()
        if country:
            cc = _COUNTRY_HINTS.get(country)
            if cc:
                return cc
    except Exception:
        pass

    return None


def _extract_best_e164_from_text(text: str, region_hint: Optional[str]) -> Optional[str]:
    """
    Scan the text for phone-like strings, return the first valid E.164 number if found.
    """
    if not text:
        return None
    try:
        for match in phonenumbers.PhoneNumberMatcher(text, region_hint or None):
            num = match.number
            if phonenumbers.is_valid_number(num):
                return phonenumbers.format_number(num, PhoneNumberFormat.E164)
    except Exception:
        pass
    return None


def _normalize_customer_phone(data: dict, email_text: str, warnings: List[str]) -> None:
    """
    Ensure Customer Phone (if present or discoverable) is stored in E.164 format.
    Priority:
      1) If LLM provided a phone, try to parse/normalize it.
      2) Else, mine the email body for a valid phone.
    """
    try:
        region = _infer_region_from_data(data)
        raw = data.get("Customer Phone")

        candidate = None
        if isinstance(raw, str) and raw.strip():
            try:
                num = phonenumbers.parse(raw.strip(), region or None)
                if phonenumbers.is_valid_number(num):
                    candidate = phonenumbers.format_number(num, PhoneNumberFormat.E164)
                else:
                    candidate = _extract_best_e164_from_text(email_text, region)
            except NumberParseException:
                candidate = _extract_best_e164_from_text(email_text, region)
        else:
            candidate = _extract_best_e164_from_text(email_text, region)

        data["Customer Phone"] = candidate if candidate else None
    except Exception as e:
        warnings.append(f"Phone normalization error: {e}")
        data["Customer Phone"] = None


def _maybe_enrich_retailer_ref(email_text: str, data: Dict[str, Any]) -> None:
    """
    Example enrichment: detect Amazon ASIN if missing.
    """
    try:
        items = data.get("Items")
        if not isinstance(items, list):
            return
        asin_pat = re.compile(r"\b([A-Z0-9]{10})\b")
        for it in items:
            rr = it.get("RetailerReference")
            if rr:
                continue
            # naive ASIN check
            m = asin_pat.search(email_text)
            if m:
                it["RetailerReference"] = m.group(1)
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────────────────
# Public: LLM-driven strict JSON extraction
# ────────────────────────────────────────────────────────────────────────────────
def extract_structured_fields_strict_json(
    headers: Dict[str, str],
    email_text: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Uses the Responses API to extract structured fields.
    Returns (data, warnings).
    """
    warnings: List[str] = []

    hdr_from = headers.get("From", "")
    hdr_to = headers.get("To", "")
    hdr_subject = headers.get("Subject", "")
    hdr_date = headers.get("Date", "")

    prompt = STRICT_PROMPT_TEMPLATE.format(
        hdr_from=hdr_from,
        hdr_to=hdr_to,
        hdr_subject=hdr_subject,
        hdr_date=hdr_date,
        email_text=email_text[:15000],  # keep prompt manageable
    )

    try:
        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )
        content = "".join(
            block.text.value
            for block in (resp.output or [])
            if getattr(block, "type", None) == "output_text"
        )
        raw_json = _extract_json_block(content)
        data = json.loads(raw_json)

        # Ensure required top-level fields exist with sane defaults
        data.setdefault("Customer Name", None)
        data.setdefault("Customer Email", None)
        data.setdefault("Customer Phone", None)
        data.setdefault("Customer Address", {})
        data.setdefault("Order Number", None)
        data.setdefault("Purchase Date", None)
        data.setdefault("Payment Method", None)
        data.setdefault("Retailer Name", None)
        data.setdefault("Locale", None)
        data.setdefault("Items", [])

        # Safety: ensure address subkeys
        addr = data.get("Customer Address") or {}
        data["Customer Address"] = {
            "Street": addr.get("Street"),
            "City": addr.get("City"),
            "Postal Code": addr.get("Postal Code"),
            "Region": addr.get("Region"),
            "Country": addr.get("Country"),
        }

        # Enrich retailer reference with ASIN if available
        _maybe_enrich_retailer_ref(email_text, data)

        # Normalize purchase prices
        _normalize_purchase_prices(data, warnings)

        # Normalize Locale to ll_CC
        _normalize_locale(data, warnings)

        # Normalize / extract Customer Phone to E.164
        _normalize_customer_phone(data, email_text, warnings)

        return data, warnings

    except Exception as e:
        warnings.append(f"LLM extraction failed: {e}")
        return {}, warnings
