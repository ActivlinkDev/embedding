# utils/email_extract.py
"""
Helpers for email ingestion:
- html_to_text: converts HTML emails to plain text
- extract_text_and_attachments_from_email_message: gets text + attachments
- extract_structured_fields_strict_json: uses GPT to extract JSON fields
  (now includes 'Locale' in ll_CC format and normalizes it)
"""

import re, json, base64
from typing import Tuple, Dict, Any, List, Optional
from bs4 import BeautifulSoup
from openai import OpenAI

# Initialize OpenAI client (uses OPENAI_API_KEY from env)
client = OpenAI()

# --- Convert HTML to plain text ---
def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["br", "p", "div"]):
        tag.append("\n")
    text = soup.get_text("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()

# --- Extract text + attachments from MIME message ---
def extract_text_and_attachments_from_email_message(msg) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    text_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []

    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = (part.get_content_type() or "").lower()
                disp = (part.get("Content-Disposition") or "").lower()

                if "attachment" in disp:
                    try:
                        payload = part.get_payload(decode=True) or b""
                        b64 = base64.b64encode(payload).decode("utf-8")
                        attachments.append({
                            "filename": part.get_filename(),
                            "content_type": ctype,
                            "data_base64": b64,
                            "size": len(payload),
                        })
                    except Exception as e:
                        warnings.append(f"Attachment decode failed: {e}")
                elif ctype in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    chunk = payload.decode(charset, errors="ignore")
                    if ctype == "text/html":
                        chunk = html_to_text(chunk)
                    text_parts.append(chunk)
        else:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            chunk = payload.decode(charset, errors="ignore")
            if (msg.get_content_type() or "").lower() == "text/html":
                chunk = html_to_text(chunk)
            text_parts.append(chunk)
    except Exception as e:
        warnings.append(f"MIME parse error: {e}")

    text = "\n\n".join(p for p in text_parts if p).strip()
    return text, attachments, warnings

# --- Prompt template for GPT extraction (with header hints) ---
STRICT_PROMPT_TEMPLATE = """Extract the following details from the order confirmation email and return strict JSON.

Top-level fields:
Customer Name
Customer Email
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

# Pattern to find an Amazon ASIN in typical order links
_ASIN_RE = re.compile(r'(?:dp|gp/product)/([A-Z0-9]{10})')

def _maybe_enrich_retailer_ref(email_text: str, data: dict) -> None:
    """If retailer reference is missing, fill with ASIN from links when present."""
    try:
        m = _ASIN_RE.search(email_text or "")
        asin = m.group(1) if m else None
        if not asin or not data or not isinstance(data, dict):
            return
        items = data.get("Items") or []
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                rr = item.get("RetailerReference")
                if rr in (None, "", "null") and asin:
                    item["RetailerReference"] = asin
    except Exception:
        # Soft-fail; enrichment is optional.
        pass

def _normalize_purchase_prices(data: dict, warnings: List[str]) -> None:
    """Ensure Purchase Price fields are numeric and currency codes are standardized, tolerate weird keys."""
    try:
        items = data.get("Items", [])
        if not isinstance(items, list):
            return
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            pp = item.get("Purchase Price")
            if isinstance(pp, dict):
                # Debug log raw keys
                print(f"[DEBUG] Item {idx} Purchase Price keys before normalize:", list(pp.keys()))
                # Fuzzy key matching
                amt_key = next((k for k in pp.keys() if "amount" in k.lower()), None)
                cur_key = next((k for k in pp.keys() if "curr" in k.lower()), None)

                if amt_key:
                    amt = pp.get(amt_key)
                    try:
                        amt_clean = str(amt).replace("£", "").strip()
                        pp[amt_key] = float(amt_clean)
                    except Exception:
                        warnings.append(f"Failed to normalize Amount '{amt}'")
                        pp[amt_key] = None

                if cur_key:
                    cur = pp.get(cur_key)
                    if cur:
                        pp[cur_key] = str(cur).upper()
    except Exception as e:
        warnings.append(f"Normalization error: {e}")

_LOCALE_RE = re.compile(r"^\s*([A-Za-z]{2})[-_]?([A-Za-z]{2})\s*$")

def _normalize_locale(data: dict, warnings: List[str]) -> None:
    """
    Normalize Locale to ll_CC.
    Accepts variants like 'EN-gb', 'fr-fr', 'pt_BR' and coerces to 'en_GB', 'fr_FR', 'pt_BR'.
    If missing or invalid, leave as null.
    """
    try:
        locale_val = data.get("Locale")
        if not locale_val:
            return
        if not isinstance(locale_val, str):
            warnings.append(f"Locale not a string: {locale_val}")
            data["Locale"] = None
            return
        m = _LOCALE_RE.match(locale_val)
        if not m:
            # Try to handle full language/country names e.g. "English (United Kingdom)" from model
            # Leave as-is but warn, then null it to avoid downstream issues.
            warnings.append(f"Unrecognized Locale format '{locale_val}', expected ll_CC")
            data["Locale"] = None
            return
        lang, country = m.group(1), m.group(2)
        data["Locale"] = f"{lang.lower()}_{country.upper()}"
    except Exception as e:
        warnings.append(f"Locale normalization error: {e}")
        data["Locale"] = None

# --- GPT extraction to strict JSON ---
def extract_structured_fields_strict_json(
    email_text: str,
    hdr_from: str = "",
    hdr_to: str = "",
    hdr_subject: str = "",
    hdr_date: str = ""
) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    prompt = STRICT_PROMPT_TEMPLATE.format(
        email_text=email_text,
        hdr_from=hdr_from or "",
        hdr_to=hdr_to or "",
        hdr_subject=hdr_subject or "",
        hdr_date=hdr_date or "",
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},  # Force JSON output
        )
        raw_json = resp.choices[0].message.content
        print("[DEBUG] Raw GPT output:", raw_json)

        if not raw_json:
            warnings.append("No response from LLM")
            return {}, warnings

        data = json.loads(raw_json)

        # Debug log parsed structure keys
        print("[DEBUG] Parsed top-level keys:", list(data.keys()))
        for idx, item in enumerate(data.get("Items", [])):
            if isinstance(item, dict):
                print(f"[DEBUG] Item {idx} keys:", list(item.keys()))
                pp = item.get("Purchase Price")
                if isinstance(pp, dict):
                    print(f"[DEBUG] Item {idx} Purchase Price keys:", list(pp.keys()))

        # Enrich retailer reference with ASIN if available
        _maybe_enrich_retailer_ref(email_text, data)

        # Normalize purchase prices
        _normalize_purchase_prices(data, warnings)

        # Normalize Locale to ll_CC
        _normalize_locale(data, warnings)

        return data, warnings

    except Exception as e:
        warnings.append(f"LLM extraction failed: {e}")
        return {}, warnings
