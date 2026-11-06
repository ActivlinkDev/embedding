"""The public API reference page: intro guide, sidebar grouping and the HTML viewer.

`main.py` serves this at /docs (only when ENABLE_API_DOCS=true). The page is the Scalar viewer
pointed at /openapi.json, restyled in Activlink colours with the viewer's own branding and
extras hidden, so readers see Activlink docs rather than tooling.

Like utils/api_docs.py this is documentation only: nothing here runs while an API request is
being served, so editing it cannot change how an endpoint behaves.
"""

import json
import os
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

# Exact version, so a viewer release can never change the page under us. Bump deliberately.
SCALAR_VERSION = "1.74.0"
SCALAR_SCRIPT = f"https://cdn.jsdelivr.net/npm/@scalar/api-reference@{SCALAR_VERSION}"

PUBLIC_BASE_URL = (os.getenv("FASTAPI_BASE_URL") or "https://api.activlink.io").rstrip("/")


# --- Sidebar ----------------------------------------------------------------------------
# Tag order here is the order sections appear in. Every tag a router uses must be listed, or
# its endpoints fall outside the groups below (tests/test_api_reference.py checks this).

OPENAPI_TAGS: List[Dict[str, str]] = [
    {"name": "Auth", "description": "Exchange ServiceClient credentials for a short-lived bearer token, and manage those credentials."},
    {"name": "Localization", "description": "The locales the platform operates in, and each locale's currency, language and operating parameters."},
    {"name": "Catalog", "description": "Device categories, the master product catalogue, and each client's own SKUs."},
    {"name": "Enrichment", "description": "Product data from Icecat, Go-UPC, EPREL and DataforSEO, and AI extraction from photos."},
    {"name": "CMS", "description": "Localised page copy and marketing content from the CMS."},
    {"name": "Devices", "description": "Register customer devices and read registrations back."},
    {"name": "Assignments", "description": "Work out which cover products a registered device qualifies for."},
    {"name": "Quotes", "description": "Price cover for a device and store the result as a quote."},
    {"name": "Basket", "description": "Collect several devices' cover into one basket, price it and pay for it in one checkout."},
    {"name": "Payments", "description": "Rate requests, Stripe Checkout sessions and payment links."},
    {"name": "Contracts", "description": "Read, cancel and resend the cover contracts issued after payment."},
    {"name": "Customers", "description": "Find or create customers, verify them, and attach devices to them."},
    {"name": "Service", "description": "Repair journeys: report a fault, attach photos, find availability and book an engineer."},
    {"name": "QR", "description": "Generate QR codes, bind them to devices and resolve scans."},
    {"name": "Messaging", "description": "SMS, one-time codes and receipt-email ingestion."},
    {"name": "Portal", "description": "Client portal sign-in, users and branding."},
    {"name": "Operations", "description": "Health checks and internal maintenance utilities."},
    {"name": "QA", "description": "Call-recording transcription and scoring."},
]

# Scalar renders these as headed sections in the sidebar.
TAG_GROUPS: List[Dict[str, Any]] = [
    {"name": "Getting started", "tags": ["Auth", "Localization"]},
    {"name": "Products", "tags": ["Catalog", "Enrichment", "CMS"]},
    {"name": "Registration and cover", "tags": ["Devices", "Assignments", "Quotes", "Basket", "Payments", "Contracts"]},
    {"name": "Customers and service", "tags": ["Customers", "Service", "QR", "Messaging"]},
    {"name": "Administration", "tags": ["Portal", "Operations", "QA"]},
]


# --- Intro guide ------------------------------------------------------------------------
# Each `##` heading becomes its own entry in the sidebar, above the endpoint groups.

DESCRIPTION = f"""
The Activlink API powers device registration, product protection quotes and payments for
Activlink's clients. Register a customer's device, find the cover it qualifies for, price it,
take payment through Stripe, and manage the contract and any repair that follows.

**Base URL:** `{PUBLIC_BASE_URL}`

All requests and responses are JSON unless an endpoint says otherwise. Almost every endpoint
needs a bearer token; each endpoint's page shows whether it does.

## Quickstart

1. Ask Activlink for a **ServiceClient** credential, a `client_id` and a `client_secret`. It is
   normally pinned to your `ClientKey`, the identifier of your tenant.
2. Exchange it for an access token. Do this from your server: the secret must never reach a
   browser or a mobile app.

```bash
curl -X POST {PUBLIC_BASE_URL}/auth/token \\
  -H "Content-Type: application/json" \\
  -d '{{"client_id": "your-client-id", "client_secret": "your-client-secret"}}'
```

```json
{{ "access_token": "eyJhbGciOi…", "token_type": "bearer", "expires_in": 2700, "scopes": ["sku:read"] }}
```

3. Call any endpoint with the token, for example to read your own client record, addressed by
   your `ClientKey`:

```bash
curl "{PUBLIC_BASE_URL}/get-client?clientkey=$ACTIVLINK_CLIENT_KEY" \\
  -H "Authorization: Bearer $ACTIVLINK_TOKEN"
```

4. List the locales you can quote in with `GET /locales`, then follow the sections below to
   register a device, quote cover for it and take payment.

## Authentication

Send the token from `POST /auth/token` on every request:

```
Authorization: Bearer <access_token>
```

* Tokens last `expires_in` seconds (45 minutes by default) and cannot be refreshed. Cache one
  and request a new one before it expires, for example at 80% of its lifetime, rather than
  exchanging credentials on every call.
* A `401` means the token is malformed, expired or was issued by another environment. Request
  a new token once and retry; if that fails too, stop and check your credentials.
* A request with no `Authorization` header at all is refused with
  `{{"detail": "Not authenticated"}}`.
* Credentials are rotated with `POST /auth/clients/{{client_id}}/rotate`. The new secret is shown
  once and only its hash is stored, so a lost secret is rotated, never recovered.

## Tenant scoping

Activlink is multi-tenant: each client's devices, quotes, contracts and customers are kept
apart by its `ClientKey`.

* A credential **pinned** to a `ClientKey` may only name that `ClientKey`, in the path, the
  query string or the body. Naming any other returns
  `403 {{"detail": "Caller is scoped to ClientKey …"}}`.
* Records reached by id (a contract, an order, a quote) that belong to another tenant return
  `404`, not `403`, so a response never confirms that someone else's record exists.
* Only Activlink's own services hold the `clientkey:*` scope that reaches every tenant. A
  credential with neither a pinned `ClientKey` nor that scope reaches no tenant data at all.

## Locales

Locales are underscore-separated language and region codes: `en_GB`, `fr_FR`, `de_DE`,
`es_ES`, `it_IT`, `fr_BE`, `nl_BE`. A locale sets the currency, language, guarantee period and
pricing a quote uses, so send the customer's locale rather than a default. `GET /locales` lists
every locale the platform knows.

## Errors

Errors use HTTP status codes and a JSON body with a `detail` field:

```json
{{ "detail": "Invalid clientKey" }}
```

| Status | Meaning |
| --- | --- |
| `400` | The request is well formed but a value is not acceptable, for example an unknown id format. |
| `401` | The bearer token is invalid or expired. Request a new one from `POST /auth/token`. |
| `403` | The token may not act on this `ClientKey` or record, or no token was sent at all. |
| `404` | Nothing matches, or the record belongs to another tenant. |
| `409` | The request conflicts with existing data, for example a SKU or client id that already exists. |
| `422` | Schema validation failed. `detail` is a list, and each entry's `loc` points at the field. |
| `502` | A service this endpoint depends on (Stripe, Icecat, the CMS…) failed or timed out. |

Treat `502` and network timeouts as retryable for reads. Do not blindly retry a write that
creates a payment, a quote or a registration: read back first to check whether it went through.

## Payments

Prices are always worked out on the server from a stored quote or basket. You never send an
amount: you send the quote or basket id and the option the customer chose, and get back a Stripe
Checkout link.

* Redirecting the customer to your success page is not proof of payment. Confirm it with
  `GET /basket/payment/session/{{session_id}}`, or wait for the contract to appear under
  `GET /contracts`.
* Contracts are issued from Stripe's own webhook, so one can appear shortly after the
  customer returns from Checkout.

## Going live

* Use one credential per service that calls the API, pinned to your `ClientKey`, with only the
  scopes it needs.
* Keep the `client_secret` in your server's secret store. Never put it, or a token, in browser
  code, a mobile app or a log.
* Cache tokens, and handle `401` by requesting a new token once before giving up.
* Send the customer's real locale on every quote and registration.
* Confirm payments on the server, never from the Checkout redirect alone.
* Log the `detail` of every error response you receive: it is what Activlink support needs to
  find the request.
"""


def build_openapi(app: FastAPI) -> Dict[str, Any]:
    """FastAPI's schema plus the intro guide, the sidebar groups and a described bearer scheme."""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        summary="Device registration, cover quotes and payments for Activlink clients.",
        description=DESCRIPTION,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    schema["x-tagGroups"] = TAG_GROUPS
    bearer = schema.get("components", {}).get("securitySchemes", {}).get("HTTPBearer")
    if bearer is not None:
        bearer["description"] = "The `access_token` returned by `POST /auth/token`."
        bearer["bearerFormat"] = "JWT"

    app.openapi_schema = schema
    return schema


# --- The page ----------------------------------------------------------------------------
# Colours follow the frontend's brand tokens (tailwind.config.ts): ivory/oat/cloud surfaces,
# ink text and the clay accent.

_THEME_CSS = """
:root, .light-mode {
  --scalar-font: "Inter", system-ui, sans-serif;
  --scalar-font-code: "JetBrains Mono", ui-monospace, monospace;
  --scalar-color-1: #29261B; --scalar-color-2: #4f4b40; --scalar-color-3: #6E6B63;
  --scalar-color-accent: #BD5B3D;
  --scalar-background-1: #FAF9F5; --scalar-background-2: #F0EEE6; --scalar-background-3: #E8E6DC;
  --scalar-background-accent: #f7e6df;
  --scalar-border-color: #E8E6DC;
  --scalar-radius: 8px; --scalar-radius-lg: 10px; --scalar-radius-xl: 12px;
  --scalar-sidebar-background-1: #F5F3EC; --scalar-sidebar-item-hover-background: #ECE9DF;
  --scalar-sidebar-item-active-background: #f5e1d8; --scalar-sidebar-color-active: #9c4528;
  --scalar-sidebar-border-color: #E8E6DC;
  --scalar-custom-header-height: 56px;
}
.dark-mode {
  --scalar-color-1: #F2EFE7; --scalar-color-2: #CBC6BA; --scalar-color-3: #9C998F;
  --scalar-color-accent: #E89373;
  --scalar-background-1: #191814; --scalar-background-2: #211f1a; --scalar-background-3: #2b2922;
  --scalar-background-accent: #3a2820;
  --scalar-border-color: #34312a;
  --scalar-sidebar-background-1: #1c1b17; --scalar-sidebar-item-hover-background: #26241f;
  --scalar-sidebar-item-active-background: #3d2a21; --scalar-sidebar-color-active: #f2b49c;
  --scalar-sidebar-border-color: #34312a;
}
a[href*="scalar.com"], .darklight-reference-promo, .darklight-reference, .generate-sdk-button, .agent-button-container { display: none !important; }
"""


def reference_html(openapi_url: str = "/openapi.json") -> str:
    """The standalone /docs page."""
    configuration = {
        "url": openapi_url,
        "theme": "none",
        "withDefaultFonts": False,
        "documentDownloadType": "none",
        "hideClientButton": True,
        "hideModels": True,
        "showDeveloperTools": "never",
        "agent": {"disabled": True},
        "defaultOpenAllTags": False,
        "authentication": {"preferredSecurityScheme": "HTTPBearer"},
        "customCss": _THEME_CSS,
        "metaData": {"title": "Activlink API reference"},
    }
    # json.dumps output is safe inside a <script> once "</" cannot close the tag early.
    config_json = json.dumps(configuration).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Activlink API reference</title>
  <meta name="description" content="The Activlink API: device registration, cover quotes and payments, with examples for every operation." />
  <meta name="robots" content="noindex" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" />
  <style>
    body {{ margin: 0; background: #FAF9F5; }}
    .al-top {{ position: sticky; top: 0; z-index: 30; display: flex; align-items: center; justify-content: space-between; gap: 16px;
      height: 56px; padding: 0 20px; box-sizing: border-box; background: #FAF9F5; border-bottom: 1px solid #E8E6DC;
      font: 500 14px/1 Inter, system-ui, sans-serif; }}
    .al-top a {{ color: #29261B; text-decoration: none; }}
    .al-brand {{ display: flex; align-items: center; gap: 10px; }}
    .al-brand strong {{ font-weight: 700; font-size: 17px; letter-spacing: -0.01em; }}
    .al-brand strong span {{ color: #D97757; }}
    .al-brand em {{ font-style: normal; color: #6E6B63; padding-left: 10px; border-left: 1px solid #E8E6DC; }}
    .al-links {{ display: flex; gap: 20px; }}
    .al-links a:hover {{ color: #BD5B3D; }}
    @media (max-width: 640px) {{ .al-links .al-hide-sm {{ display: none; }} }}
    body.dark-mode {{ background: #191814; }}
    body.dark-mode .al-top {{ background: #191814; border-color: #34312a; }}
    body.dark-mode .al-top a {{ color: #F2EFE7; }}
    body.dark-mode .al-brand em {{ color: #9C998F; border-color: #34312a; }}
    body.dark-mode .al-links a:hover {{ color: #E89373; }}
  </style>
</head>
<body>
  <header class="al-top">
    <a class="al-brand" href="https://activlink.io"><strong>Activ<span>link</span></strong><em>API reference</em></a>
    <nav class="al-links">
      <a class="al-hide-sm" href="#description/quickstart">Quickstart</a>
      <a class="al-hide-sm" href="#description/authentication">Authentication</a>
      <a href="#description/going-live">Going live</a>
    </nav>
  </header>
  <div id="al-reference"></div>
  <script src="{SCALAR_SCRIPT}"></script>
  <script>
    var config = {config_json};
    // Code samples and Test Request target whichever host is serving these docs.
    config.servers = [{{ url: window.location.origin }}];
    Scalar.createApiReference("#al-reference", config);
    // Activlink docs, not tooling: hide the viewer's own extras (AI chat, SDK generator,
    // developer toolbar, spec version badges).
    var HIDE = /^(Ask AI|Ask AI Agent|Generate SDK|Generate MCP|Connect MCP|Developer Tools|OpenAPI \\d[\\d.]*|v\\d+\\.\\d+\\.\\d+)$/;
    function tidy() {{
      document.querySelectorAll('button, .badge, a').forEach(function (el) {{
        if (HIDE.test(el.textContent.trim())) el.style.display = 'none';
      }});
    }}
    new MutationObserver(tidy).observe(document.body, {{ childList: true, subtree: true }});
  </script>
</body>
</html>
"""
