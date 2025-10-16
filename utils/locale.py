"""Locale utility helpers for FastAPI services.

Provides:
- get_locale_mapping(): returns dict mapping short keys (en, es, etc.) to structure {fastapi, cms, label}
- map_fastapi_to_strapi(fastapi_locale): converts e.g. en_GB -> en-GB
- map_strapi_to_fastapi(strapi_locale): converts e.g. en-GB -> en_GB
- extract_language_code(locale): returns base language code (en, es, fr, etc.)

Includes simple in-process cache; update here if adding more locales.
"""
from functools import lru_cache
from typing import Dict, Any, Tuple

_LOCALE_MAP = {
    "en": {"fastapi": "en_GB", "cms": "en-GB", "label": "English"},
    "en_IE": {"fastapi": "en_IE", "cms": "en-IE", "label": "English (Ireland)"},
    "es": {"fastapi": "es_ES", "cms": "es-ES", "label": "Español"},
    "it": {"fastapi": "it_IT", "cms": "it-IT", "label": "Italiano"},
    "fr": {"fastapi": "fr_FR", "cms": "fr-FR", "label": "Français"},
    "de": {"fastapi": "de_DE", "cms": "de-DE", "label": "Deutsch"},
    "fr_BE": {"fastapi": "fr_BE", "cms": "fr-BE", "label": "Français (Belgique)"},
    "nl_BE": {"fastapi": "nl_BE", "cms": "nl-BE", "label": "Nederlands (België)"},
    # --- Additional major EU languages / locales ---
    "pt": {"fastapi": "pt_PT", "cms": "pt-PT", "label": "Português"},
    "pt_BR": {"fastapi": "pt_BR", "cms": "pt-BR", "label": "Português (Brasil)"},  # often useful
    "nl": {"fastapi": "nl_NL", "cms": "nl-NL", "label": "Nederlands"},
    "pl": {"fastapi": "pl_PL", "cms": "pl-PL", "label": "Polski"},
    "sv": {"fastapi": "sv_SE", "cms": "sv-SE", "label": "Svenska"},
    "da": {"fastapi": "da_DK", "cms": "da-DK", "label": "Dansk"},
    "fi": {"fastapi": "fi_FI", "cms": "fi-FI", "label": "Suomi"},
    "cs": {"fastapi": "cs_CZ", "cms": "cs-CZ", "label": "Čeština"},
    "sk": {"fastapi": "sk_SK", "cms": "sk-SK", "label": "Slovenčina"},
    "sl": {"fastapi": "sl_SI", "cms": "sl-SI", "label": "Slovenščina"},
    "hr": {"fastapi": "hr_HR", "cms": "hr-HR", "label": "Hrvatski"},
    "ro": {"fastapi": "ro_RO", "cms": "ro-RO", "label": "Română"},
    "bg": {"fastapi": "bg_BG", "cms": "bg-BG", "label": "Български"},
    "hu": {"fastapi": "hu_HU", "cms": "hu-HU", "label": "Magyar"},
    "el": {"fastapi": "el_GR", "cms": "el-GR", "label": "Ελληνικά"},
    "et": {"fastapi": "et_EE", "cms": "et-EE", "label": "Eesti"},
    "lv": {"fastapi": "lv_LV", "cms": "lv-LV", "label": "Latviešu"},
    "lt": {"fastapi": "lt_LT", "cms": "lt-LT", "label": "Lietuvių"},
    "ga": {"fastapi": "ga_IE", "cms": "ga-IE", "label": "Gaeilge"},
    "mt": {"fastapi": "mt_MT", "cms": "mt-MT", "label": "Malti"},
    # Optional / candidate (non-official EU but often needed)
    "tr": {"fastapi": "tr_TR", "cms": "tr-TR", "label": "Türkçe"},
    "no": {"fastapi": "nb_NO", "cms": "nb-NO", "label": "Norsk"},
}

@lru_cache(maxsize=1)
def get_locale_mapping() -> Dict[str, Dict[str, str]]:
    return _LOCALE_MAP

def map_fastapi_to_strapi(fastapi_locale: str) -> str:
    return fastapi_locale.replace('_', '-')

def map_strapi_to_fastapi(strapi_locale: str) -> str:
    return strapi_locale.replace('-', '_')

def extract_language_code(locale: str) -> str:
    return (locale.split('_')[0].split('-')[0]).lower()

class LocaleNotSupportedError(ValueError):
    pass

def resolve_strapi_locale(fastapi_locale: str, mongo_lookup: Dict[str, Any] | None = None) -> Tuple[str, str]:
    """Return (fastapi_locale_normalized, strapi_locale) or raise LocaleNotSupportedError.

    mongo_lookup may be a document like {"locale": "en_GB", "strapi_locale": "en-GB"} already fetched.
    If mongo_lookup not provided, falls back to simple replacement logic.
    """
    if mongo_lookup:
        strapi_locale = mongo_lookup.get("strapi_locale")
        if not strapi_locale:
            raise LocaleNotSupportedError(f"Locale '{fastapi_locale}' is not supported or has no strapi_locale mapping.")
        return fastapi_locale, strapi_locale
    # Fallback simple transform
    return fastapi_locale, map_fastapi_to_strapi(fastapi_locale)

__all__ = [
    "get_locale_mapping",
    "map_fastapi_to_strapi",
    "map_strapi_to_fastapi",
    "extract_language_code",
    "resolve_strapi_locale",
    "LocaleNotSupportedError",
]
