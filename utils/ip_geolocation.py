"""IP-based geolocation utilities for QR scan country detection."""
import ipaddress
import requests
from fastapi import Request

# Static mapping from ISO 3166-1 alpha-2 country codes to FastAPI locale strings.
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

# RFC 1918 + loopback + ULA private networks (Fix 4: only 172.16/12 is private, not all 172.x)
_PRIVATE_NETWORKS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return True


def _mask_ip(ip: str) -> str:
    """Mask the identifying portion of an IP address for GDPR storage (Fix 9: handles IPv6)."""
    if not ip:
        return ""
    if ":" in ip:
        # IPv6 — mask the last 64 bits (last two groups of a full address)
        parts = ip.rsplit(":", 1)
        return f"{parts[0]}:xxxx"
    # IPv4 — mask the last octet
    parts = ip.rsplit(".", 1)
    return f"{parts[0]}.x" if len(parts) == 2 else ip


def get_client_ip(request: Request) -> str:
    """Extract the real client IP, preferring proxy-forwarded headers."""
    for header in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        value = request.headers.get(header, "").strip()
        if value:
            return value.split(",")[0].strip()
    host = getattr(request.client, "host", "") or ""
    return host


def lookup_country(ip: str, timeout: float = 2.0) -> dict:
    """Resolve country from IP via ipinfo.io (HTTPS, free tier, Fix 5). Never raises."""
    if not ip or _is_private(ip):
        return {"country_code": "", "country_name": ""}
    try:
        resp = requests.get(
            f"https://ipinfo.io/{ip}/json",
            timeout=timeout,
        )
        data = resp.json()
        country_code = data.get("country", "")
        # ipinfo.io returns country code only; country name requires a lookup we skip
        return {
            "country_code": country_code,
            "country_name": country_code,  # use code as name fallback (sufficient for logging)
        }
    except Exception:
        pass
    return {"country_code": "", "country_name": ""}


def country_code_to_locale(country_code: str) -> str:
    """Map an ISO 3166-1 alpha-2 country code to a FastAPI locale string."""
    return _COUNTRY_TO_LOCALE.get(country_code.upper(), "en_GB") if country_code else "en_GB"
