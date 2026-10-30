"""Per-client email settings, branding and localised copy.

Everything a confirmation email needs is resolved here in one place, so a sender only has to
say *which* client and locale it is writing for:

- **Settings** come from ``ClientEmailConfig`` (edited in the admin portal Email tab).
  Emails are **off** for a client until that tab enables them, so deploying this code sends
  nothing to any client's customers by surprise.
- **Branding** falls back from those settings to the client's existing ``ClientKey.Styles``.
- **Copy** comes from the Strapi ``email-copy`` single type for the locale, field by field,
  falling back to built-in English. A missing translation degrades to English; it never
  blocks a send.
- **Template**: the client's Brevo template id for this kind, else the platform default from
  ``BREVO_TEMPLATE_<KIND>``, else the built-in HTML layout in ``render_fallback_html``.
"""
from __future__ import annotations

import html
import os
import sys
import time
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from routers.comms import store

Kind = Literal["registration", "plan", "otp"]
KINDS = ("registration", "plan", "otp")

MAIL_FROM = os.getenv("MAIL_FROM", "no-reply@activlink.io")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Activlink")
STRAPI_BASE = (os.getenv("STRAPI_BASE_URL") or "").rstrip("/")
STRAPI_BEARER_TOKEN = os.getenv("STRAPI_BEARER_TOKEN")
DEFAULT_PRIMARY = "#1F2937"


def allowed_sender_domains() -> set[str]:
    """Domains verified in Brevo. A sender on any other domain would fail DMARC, so it is
    refused at save time and ignored at send time. MAIL_FROM's own domain is always allowed."""
    configured = {d.strip().lower() for d in (os.getenv("BREVO_ALLOWED_SENDER_DOMAINS") or "").split(",") if d.strip()}
    configured.add(MAIL_FROM.rpartition("@")[2].lower())
    return configured


def default_template_id(kind: str) -> Optional[int]:
    raw = os.getenv(f"BREVO_TEMPLATE_{kind.upper()}")
    try:
        value = int(raw) if raw else None
    except ValueError:
        return None
    return value if value and value > 0 else None


# ---------------------------------------------------------------------------
# Settings model (validated on save from the portal)
# ---------------------------------------------------------------------------

class EnabledKinds(BaseModel):
    registration: bool = False
    plan: bool = False


class TemplateIds(BaseModel):
    registration: Optional[int] = Field(None, gt=0)
    plan: Optional[int] = Field(None, gt=0)
    otp: Optional[int] = Field(None, gt=0)


class EmailSettings(BaseModel):
    enabled: EnabledKinds = Field(default_factory=EnabledKinds)
    senderEmail: Optional[str] = Field(None, max_length=254)
    senderName: Optional[str] = Field(None, max_length=70)
    replyTo: Optional[str] = Field(None, max_length=254)
    templateIds: TemplateIds = Field(default_factory=TemplateIds)
    logoUrl: Optional[str] = Field(None, max_length=1000)
    primaryColour: Optional[str] = None
    buttonColour: Optional[str] = None
    footerText: Optional[str] = Field(None, max_length=500)

    @field_validator("senderEmail", "replyTo", "senderName", "logoUrl", "primaryColour",
                     "buttonColour", "footerText", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("senderEmail")
    @classmethod
    def _sender_on_verified_domain(cls, value):
        if value is None:
            return value
        email = store.deliverable_email(value)
        if not email:
            raise ValueError("senderEmail is not a valid email address")
        if email.rpartition("@")[2].lower() not in allowed_sender_domains():
            raise ValueError("senderEmail must use a domain verified in Brevo")
        return email

    @field_validator("replyTo")
    @classmethod
    def _valid_reply_to(cls, value):
        if value is None:
            return value
        email = store.deliverable_email(value)
        if not email:
            raise ValueError("replyTo is not a valid email address")
        return email

    @field_validator("logoUrl")
    @classmethod
    def _https_logo(cls, value):
        if value is not None and not value.lower().startswith("https://"):
            raise ValueError("logoUrl must be an https:// URL")
        return value

    @field_validator("primaryColour", "buttonColour")
    @classmethod
    def _hex(cls, value):
        if value is not None and not store.is_hex_colour(value):
            raise ValueError("Colours must be hex, e.g. #0B5FFF")
        return value


def load_settings(client_key: str) -> EmailSettings:
    doc = store.email_config.find_one({"client_key": client_key}, {"_id": 0, "settings": 1}) or {}
    try:
        return EmailSettings.model_validate(doc.get("settings") or {})
    except Exception:
        # A hand-edited or legacy row must not break sending for the client: fall back to
        # defaults, which means "disabled", and say so in the logs.
        print(f"[EMAIL] Invalid stored email settings for {client_key}; treating as disabled",
              file=sys.stderr)
        return EmailSettings()


# ---------------------------------------------------------------------------
# Localised copy (Strapi email-copy, English fallback)
# ---------------------------------------------------------------------------

# Mirrors Strapi translations/email-copy.json "en". Used field by field when Strapi is
# unreachable, the locale is unpublished, or an editor left a field blank.
DEFAULT_COPY: dict[str, str] = {
    "Greeting": "Hi {firstName},",
    "Greeting_No_Name": "Hello,",
    "Registration_Subject": "Your {clientName} product registration is confirmed",
    "Registration_Preheader": "Your product is registered. Keep this email for your records.",
    "Registration_Heading": "Thanks for registering your product",
    "Registration_Intro": "Your product is now registered with {clientName}. Keep this email: the link below takes you straight to your product's record whenever you need it.",
    "Registration_Cta": "View my product",
    "Plan_Subject": "Your {clientName} cover is confirmed",
    "Plan_Preheader": "Thank you for your purchase. Your cover details are inside.",
    "Plan_Heading": "You're covered",
    "Plan_Intro": "Thank you for your purchase from {clientName}. Your cover details are below, and you can view each plan at any time using its link.",
    "Plan_Cta": "View my plan",
    "Otp_Subject": "Your {clientName} verification code",
    "Otp_Heading": "Your verification code",
    "Otp_Intro": "Use this code to view your full details. It expires in 10 minutes. If you didn't ask for it, you can ignore this email.",
    "Label_Reference": "Reference",
    "Label_Cover": "Cover",
    "Label_Start_Date": "Start date",
    "Label_End_Date": "End date",
    "Footer": "You're receiving this email because you registered a product or bought cover with {clientName}.",
}

_COPY_TTL_SECONDS = 300
_copy_cache: dict[str, tuple[float, dict]] = {}


def _strapi_locales(locale: Optional[str]) -> list[str]:
    """``fr_BE`` -> ``["fr-BE", "fr"]``. The platform spells locales with an underscore,
    Strapi with a hyphen."""
    if not locale:
        return []
    code = str(locale).replace("_", "-")
    base = code.split("-")[0]
    return [code] if base == code else [code, base]


async def _fetch_strapi_copy(strapi_locale: str) -> dict:
    if not STRAPI_BASE:
        return {}
    cached = _copy_cache.get(strapi_locale)
    if cached and time.monotonic() - cached[0] < _COPY_TTL_SECONDS:
        return cached[1]
    headers = {"Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"} if STRAPI_BEARER_TOKEN else {}
    data: dict = {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{STRAPI_BASE}/email-copy", params={"locale": strapi_locale}, headers=headers)
        if r.status_code == 200:
            body = r.json()
            data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else {}
    except Exception as exc:  # noqa: BLE001 — copy is best-effort, English is always available
        print(f"[EMAIL] Strapi email-copy unavailable for {strapi_locale}: {type(exc).__name__}", file=sys.stderr)
        return {}
    # Strapi returns the default locale for an unknown one; only trust a matching entry.
    if data.get("locale") and str(data["locale"]).lower() != strapi_locale.lower():
        data = {}
    _copy_cache[strapi_locale] = (time.monotonic(), data)
    return data


async def load_copy(locale: Optional[str]) -> dict[str, str]:
    copy = dict(DEFAULT_COPY)
    for candidate in reversed(_strapi_locales(locale)):
        fetched = await _fetch_strapi_copy(candidate)
        for key in DEFAULT_COPY:
            value = fetched.get(key)
            if isinstance(value, str) and value.strip():
                copy[key] = value
    return copy


def fill(text: str, **values: str) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value or "")
    return text


# ---------------------------------------------------------------------------
# Resolved context
# ---------------------------------------------------------------------------

class EmailContext(BaseModel):
    client_key: str
    client_id: str
    client_name: str
    locale: Optional[str]
    settings: EmailSettings
    sender_email: str
    sender_name: str
    logo_url: Optional[str]
    primary_colour: str
    button_colour: str
    link_base: str
    copy_text: dict[str, str]

    def enabled_for(self, kind: str) -> bool:
        if kind == "otp":
            return True  # verification codes are part of the Cloud ID page, not optional mail
        return bool(getattr(self.settings.enabled, kind, False))

    def template_id(self, kind: str) -> Optional[int]:
        return getattr(self.settings.templateIds, kind, None) or default_template_id(kind)


def client_key_doc(client_key: str) -> Optional[dict]:
    return store.client_keys.find_one(
        {"ClientKey": client_key},
        {"_id": 0, "ClientKey": 1, "Client_ID": 1, "Client_Name": 1, "Styles": 1, "Logo_URL": 1,
         "redirect_base_url": 1})


def client_key_for(client_id: Optional[str], source: Optional[str]) -> Optional[str]:
    """The ClientKey for a brand's channel. ``Devices`` and baskets store the brand
    (``Client_ID``) and the channel (``source``), not the key."""
    if not client_id:
        return None
    query = {"Client_ID": client_id}
    doc = None
    if source:
        doc = store.client_keys.find_one({**query, "Source": source}, {"ClientKey": 1})
    doc = doc or store.client_keys.find_one(query, {"ClientKey": 1}, sort=[("_id", 1)])
    return (doc or {}).get("ClientKey")


def _link_base(doc: dict) -> str:
    base = doc.get("redirect_base_url") or os.getenv("FRONTEND_BASE_URL") or ""
    base = str(base).strip().rstrip("/")
    return base if base.lower().startswith("https://") or base.startswith("http://localhost") else ""


async def resolve_email_context(client_key: str, locale: Optional[str]) -> Optional[EmailContext]:
    doc = client_key_doc(client_key)
    if not doc or not doc.get("Client_ID"):
        return None
    settings = load_settings(client_key)
    styles = doc.get("Styles") if isinstance(doc.get("Styles"), dict) else {}

    sender_email = MAIL_FROM
    if settings.senderEmail and settings.senderEmail.rpartition("@")[2].lower() in allowed_sender_domains():
        sender_email = settings.senderEmail
    client_name = doc.get("Client_Name") or doc.get("Client_ID")

    # Client Styles tokens as the portal's Client Styles tab writes them (Logo_URL, Accent,
    # ButtonBackground), plus the older camelCase spellings some tenants still carry.
    style_logo = next((styles[k] for k in ("Logo_URL", "StylesLogo_URL", "logoUrl")
                       if isinstance(styles.get(k), str) and styles[k].lower().startswith("https://")), None)
    logo = settings.logoUrl or style_logo or (
        doc.get("Logo_URL") if isinstance(doc.get("Logo_URL"), str) and doc["Logo_URL"].lower().startswith("https://") else None)
    primary = settings.primaryColour or next(
        (styles[k] for k in ("Accent", "primaryColour", "HeaderAccent") if store.is_hex_colour(styles.get(k))),
        DEFAULT_PRIMARY)
    button = settings.buttonColour or next(
        (styles[k] for k in ("ButtonBackground",) if store.is_hex_colour(styles.get(k))), primary)

    return EmailContext(
        client_key=client_key,
        client_id=doc["Client_ID"],
        client_name=client_name,
        locale=locale,
        settings=settings,
        sender_email=sender_email,
        sender_name=settings.senderName or client_name or MAIL_FROM_NAME,
        logo_url=logo,
        primary_colour=primary,
        button_colour=button,
        link_base=_link_base(doc),
        copy_text=await load_copy(locale),
    )


def build_params(ctx: EmailContext, kind: str, *, name: str = "", items: Optional[list] = None,
                 code: Optional[str] = None) -> dict:
    """The params contract every Brevo template (platform default or client-owned) receives.
    Documented in routers/comms/README.md — change both together."""
    prefix = {"registration": "Registration", "plan": "Plan", "otp": "Otp"}[kind]
    c = ctx.copy_text
    first = store.first_name(name)
    values = {"clientName": ctx.client_name, "firstName": first}
    copy = {
        "subject": fill(c[f"{prefix}_Subject"], **values),
        "heading": fill(c[f"{prefix}_Heading"], **values),
        "intro": fill(c[f"{prefix}_Intro"], **values),
        "greeting": fill(c["Greeting"], **values) if first else c["Greeting_No_Name"],
        "footer": ctx.settings.footerText or fill(c["Footer"], **values),
        "labels": {
            "reference": c["Label_Reference"],
            "cover": c["Label_Cover"],
            "startDate": c["Label_Start_Date"],
            "endDate": c["Label_End_Date"],
        },
    }
    if kind != "otp":
        copy["preheader"] = fill(c[f"{prefix}_Preheader"], **values)
        copy["cta"] = c[f"{prefix}_Cta"]
    params = {
        "kind": kind,
        "locale": ctx.locale or "",
        "clientName": ctx.client_name,
        "logoUrl": ctx.logo_url or "",
        "primaryColour": ctx.primary_colour,
        "buttonColour": ctx.button_colour,
        "firstName": first,
        "copy": copy,
        "items": items or [],
    }
    if code is not None:
        params["code"] = code
    return params


def render_fallback_html(params: dict) -> str:
    """A plain branded layout, used only when neither the client nor the platform has a Brevo
    template for this kind. Every value is escaped: names and product titles are customer or
    catalogue data."""
    e = lambda v: html.escape(str(v or ""), quote=True)  # noqa: E731
    copy = params["copy"]
    colour = params["primaryColour"] if store.is_hex_colour(params["primaryColour"]) else DEFAULT_PRIMARY
    button = params["buttonColour"] if store.is_hex_colour(params["buttonColour"]) else colour
    logo = (f'<img src="{e(params["logoUrl"])}" alt="{e(params["clientName"])}" style="max-height:48px;max-width:200px">'
            if params.get("logoUrl") else f'<strong style="font-size:20px">{e(params["clientName"])}</strong>')
    rows = []
    labels = copy["labels"]
    for item in params.get("items") or []:
        title = item.get("title") or " ".join(x for x in (item.get("make"), item.get("model")) if x)
        lines = [f'<div style="font-weight:600;font-size:16px">{e(title)}</div>']
        if item.get("coverName"):
            lines.append(f'<div>{e(labels["cover"])}: {e(item["coverName"])}</div>')
        if item.get("reference"):
            lines.append(f'<div>{e(labels["reference"])}: {e(item["reference"])}</div>')
        if item.get("startDate"):
            lines.append(f'<div>{e(labels["startDate"])}: {e(item["startDate"])}</div>')
        if item.get("endDate"):
            lines.append(f'<div>{e(labels["endDate"])}: {e(item["endDate"])}</div>')
        if item.get("cloudUrl") and copy.get("cta"):
            lines.append(
                f'<div style="margin-top:12px"><a href="{e(item["cloudUrl"])}" '
                f'style="background:{button};color:#ffffff;padding:10px 18px;border-radius:6px;'
                f'text-decoration:none;display:inline-block">{e(copy["cta"])}</a></div>')
        rows.append('<div style="border:1px solid #E5E7EB;border-radius:8px;padding:16px;margin:12px 0">'
                    + "".join(lines) + "</div>")
    code = (f'<div style="font-size:32px;letter-spacing:8px;font-weight:700;margin:24px 0">{e(params["code"])}</div>'
            if params.get("code") else "")
    preheader = (f'<div style="display:none;max-height:0;overflow:hidden">{e(copy.get("preheader"))}</div>'
                 if copy.get("preheader") else "")
    return (
        '<!doctype html><html><body style="margin:0;background:#F3F4F6;font-family:Arial,Helvetica,sans-serif;color:#111827">'
        f'{preheader}'
        '<div style="max-width:600px;margin:0 auto;padding:24px">'
        f'<div style="border-top:4px solid {colour};background:#ffffff;border-radius:8px;padding:24px">'
        f'<div style="margin-bottom:20px">{logo}</div>'
        f'<h1 style="font-size:22px;margin:0 0 16px">{e(copy["heading"])}</h1>'
        f'<p>{e(copy["greeting"])}</p><p>{e(copy["intro"])}</p>'
        f'{code}{"".join(rows)}'
        '</div>'
        f'<p style="font-size:12px;color:#6B7280;margin-top:16px">{e(copy["footer"])}</p>'
        '</div></body></html>'
    )
