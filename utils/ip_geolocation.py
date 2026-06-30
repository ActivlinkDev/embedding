"""IP-based geolocation utilities for QR scan country detection."""
import requests
from fastapi import Request

# Static mapping from ISO 3166-1 alpha-2 country codes to FastAPI locale strings.
# Covers all locales in utils/locale.py plus common non-EU countries with en_GB fallback.
_COUNTRY_TO_LOCALE = {
    "GB": "en_GB",
    "IE": "en_IE",
    "FR": "fr_FR",
    "BE": "fr_BE",
    "DE": "de_DE",
    "ES": "es_ES",
    "IT": "it_IT",
    "NL": "nl_NL",
    "PT": "pt_PT",
    "BR": "pt_BR",
    "PL": "pl_PL",
    "SE": "sv_SE",
    "DK": "da_DK",
    "FI": "fi_FI",
    "CZ": "cs_CZ",
    "SK": "sk_SK",
    "SI": "sl_SI",
    "HR": "hr_HR",
    "RO": "ro_RO",
    "BG": "bg_BG",
    "HU": "hu_HU",
    "GR": "el_GR",
    "EE": "et_EE",
    "LV": "lv_LV",
    "LT": "lt_LT",
    "MT": "mt_MT",
    "TR": "tr_TR",
    "NO": "nb_NO",
}

_PRIVATE_PREFIXES = ("10.", "192.168.", "172.", "127.", "::1", "localhost")


def get_client_ip(request: Request) -> str:
    """Extract the real client IP, preferring proxy-forwarded headers."""
    for header in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        value = request.headers.get(header, "").strip()
        if value:
            return value.split(",")[0].strip()
    host = getattr(request.client, "host", "") or ""
    return host


def lookup_country(ip: str, timeout: float = 2.0) -> dict:
    """Call ip-api.com to resolve country from IP. Never raises."""
    if not ip or any(ip.startswith(p) for p in _PRIVATE_PREFIXES):
        return {"country_code": "", "country_name": ""}
    try:
        resp = requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,country,countryCode"},
            timeout=timeout,
        )
        data = resp.json()
        if data.get("status") == "success":
            return {
                "country_code": data.get("countryCode", ""),
                "country_name": data.get("country", ""),
            }
    except Exception:
        pass
    return {"country_code": "", "country_name": ""}


def country_code_to_locale(country_code: str) -> str:
    """Map an ISO 3166-1 alpha-2 country code to a FastAPI locale string."""
    return _COUNTRY_TO_LOCALE.get(country_code.upper(), "en_GB") if country_code else "en_GB"
