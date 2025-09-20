import re, json, base64
from typing import Tuple, Dict, Any, List, Optional
from bs4 import BeautifulSoup
from openai import OpenAI
import phonenumbers
from phonenumbers import PhoneNumberFormat
from email.utils import parseaddr

client = OpenAI()

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["br", "p", "div"]):
        tag.append("\n")
    text = soup.get_text("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()

def extract_text_and_attachments_from_email_message(msg) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    warnings, text_parts, attachments = [], [], []
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

STRICT_PROMPT_TEMPLATE = """Extract the following details from the order confirmation email and return strict JSON.

Top-level fields:
Customer Name
Customer Email (this should be from the {hdr_from})
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

def _maybe_enrich_retailer_ref(email_text: str, data: dict) -> None:
    m = re.search(r'(?:dp|gp/product)/([A-Z0-9]{10})', email_text or "")
    asin = m.group(1) if m else None
    if not asin:
        return
    items = data.get("Items") or []
    for item in items:
        if isinstance(item, dict) and not item.get("RetailerReference"):
            item["RetailerReference"] = asin

def _normalize_purchase_prices(data: dict, warnings: List[str]) -> None:
    try:
        items = data.get("Items", [])
        for item in items:
            pp = item.get("Purchase Price")
            if isinstance(pp, dict):
                try:
                    amt = str(pp.get("Amount", "")).replace("£", "").strip()
                    pp["Amount"] = float(amt)
                except Exception:
                    warnings.append(f"Could not parse amount: {pp.get('Amount')}")
                    pp["Amount"] = None
                pp["Currency"] = str(pp.get("Currency", "")).upper()[:3] if pp.get("Currency") else None
    except Exception as e:
        warnings.append(f"Price normalization error: {e}")

def _normalize_locale(data: dict, warnings: List[str]) -> None:
    try:
        val = data.get("Locale")
        if isinstance(val, str) and re.match(r"^[a-z]{2}_[A-Z]{2}$", val):
            return
        m = re.match(r"^([a-zA-Z]{2})[-_]?([a-zA-Z]{2})$", str(val))
        if m:
            ll, cc = m.group(1).lower(), m.group(2).upper()
            data["Locale"] = f"{ll}_{cc}"
        else:
            data["Locale"] = None
    except Exception as e:
        warnings.append(f"Locale normalization error: {e}")
        data["Locale"] = None

_COUNTRY_HINTS = {
    "united kingdom": "GB", "uk": "GB", "england": "GB",
    "united states": "US", "usa": "US",
    "france": "FR", "germany": "DE", "spain": "ES", "italy": "IT", "portugal": "PT",
    "ireland": "IE", "netherlands": "NL", "croatia": "HR", "canada": "CA"
}

def _infer_region_from_data(data: dict) -> Optional[str]:
    loc = data.get("Locale")
    if isinstance(loc, str) and len(loc) == 5 and loc[2] == "_":
        return loc.split("_")[1]
    country = ((data.get("Customer Address") or {}).get("Country") or "").lower()
    return _COUNTRY_HINTS.get(country)

def _extract_best_e164_from_text(text: str, region_hint: Optional[str]) -> Optional[str]:
    try:
        for match in phonenumbers.PhoneNumberMatcher(text, region_hint):
            num = match.number
            if phonenumbers.is_valid_number(num):
                return phonenumbers.format_number(num, PhoneNumberFormat.E164)
    except Exception:
        pass
    return None

def _normalize_customer_phone(data: dict, email_text: str, warnings: List[str]) -> None:
    try:
        raw = data.get("Customer Phone")
        region = _infer_region_from_data(data)

        candidate = None
        if isinstance(raw, str) and raw.strip():
            try:
                num = phonenumbers.parse(raw, region or None)
                if phonenumbers.is_valid_number(num):
                    candidate = phonenumbers.format_number(num, PhoneNumberFormat.E164)
            except Exception:
                pass

        if not candidate:
            candidate = _extract_best_e164_from_text(email_text, region)

        if candidate:
            data["Customer Phone"] = candidate
        else:
            data["Customer Phone"] = None
            warnings.append("No valid E.164 phone found in email")
    except Exception as e:
        warnings.append(f"Phone normalization failed: {e}")
        data["Customer Phone"] = None

def extract_structured_fields_strict_json(
    email_text: str,
    hdr_from: str = "",
    hdr_to: str = "",
    hdr_subject: str = "",
    hdr_date: str = "",
) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []

    prompt = STRICT_PROMPT_TEMPLATE.format(
        email_text=email_text[:15000],
        hdr_from=hdr_from,
        hdr_to=hdr_to,
        hdr_subject=hdr_subject,
        hdr_date=hdr_date,
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)

        # ✅ Force "Customer Email" from header
        parsed_email = parseaddr(hdr_from or "")[1]
        if parsed_email:
            data["Customer Email"] = parsed_email

        _maybe_enrich_retailer_ref(email_text, data)
        _normalize_purchase_prices(data, warnings)
        _normalize_locale(data, warnings)
        _normalize_customer_phone(data, email_text, warnings)

        return data, warnings

    except Exception as e:
        warnings.append(f"LLM extraction failed: {e}")
        return {}, warnings
