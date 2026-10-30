# Confirmation emails (Brevo) and Cloud ID links

| Email | Sent when | Hook | Dedupe key |
|---|---|---|---|
| Plan confirmation | Stripe `checkout.session.completed` has issued contracts | `routers/stripe_webook.py`, after `create_contract` | `plan:{order_reference}` |
| Registration confirmation (web) | Devices are paired to a customer on a journey with no purchase | `routers/customer/pair_customer_devices.py` | `registration:{customer_id}:{device-set hash}` |
| Registration confirmation (embedded) | `POST /register` matched at least one device | `routers/embedded_register_device.py` | `registration:embedded:{registration_id}` |
| Verification code | Customer asks to see full details on the Cloud ID page | `POST /cloud/{token}/otp/request` | per send |

Purchases get only the plan email, which already lists the covered devices.

Emails are **off per client until enabled** in the admin portal Email tab (`ClientEmailConfig`). A send is skipped and logged in `EmailLog` when it's disabled, when there is no deliverable address (including the `@placeholder.local` address the customer step invents), or when the client has no `redirect_base_url` and `FRONTEND_BASE_URL` is unset.

## Environment

| Variable | Purpose |
|---|---|
| `BREVO_API_KEY` | Brevo transactional API key. |
| `BREVO_SANDBOX` | `true` on staging: Brevo validates but delivers nothing. |
| `MAIL_FROM`, `MAIL_FROM_NAME` | Platform sender, used when a client has none. Its domain is always allowed. |
| `BREVO_ALLOWED_SENDER_DOMAINS` | Comma-separated domains verified in Brevo (SPF/DKIM). Client senders on any other domain are refused. |
| `BREVO_TEMPLATE_REGISTRATION`, `BREVO_TEMPLATE_PLAN`, `BREVO_TEMPLATE_OTP` | Platform default template ids. If none is set for a kind (and the client has none), the built-in layout in `email_config.render_fallback_html` is used. |
| `CLOUD_ACCESS_SECRET` | 32+ characters. Signs verification codes and access grants. Without it the `/cloud` router is not loaded. |
| `FRONTEND_BASE_URL` | Link base when a client has no `redirect_base_url`. |
| `STRAPI_BASE_URL`, `STRAPI_BEARER_TOKEN` | Read the `email-copy` single type. English defaults are used if Strapi is unreachable. |

## Template params contract

Platform default templates and any client-owned template receive the same `params`:

```json
{
  "kind": "registration | plan | otp",
  "locale": "en_GB",
  "clientName": "AO.com",
  "logoUrl": "https://…",
  "primaryColour": "#112233",
  "buttonColour": "#112233",
  "firstName": "Paul",
  "orderReference": "ORD-2026-000001",
  "code": "123456",
  "copy": {
    "subject": "…", "preheader": "…", "heading": "…", "intro": "…", "greeting": "Hi Paul,",
    "cta": "View my product", "footer": "…",
    "labels": { "reference": "Reference", "cover": "Cover", "startDate": "Start date", "endDate": "End date" }
  },
  "items": [
    { "title": "Washing machine", "make": "Bosch", "model": "WGG244A9GB", "category": "Laundry",
      "coverName": "3 year cover", "reference": "ACT-2026-000123", "status": "ACTIVE",
      "startDate": "2026-10-30", "endDate": "2029-10-29",
      "cloudUrl": "https://ao.registermyproduct.io/cloud/<token>?locale=en_GB" }
  ]
}
```

`orderReference` is sent for plans only, and `code` for verification emails only. `coverName`, `reference`, `status` and the dates appear on plan items only. Serial numbers, IMEI, MAC, prices, addresses and phone numbers are never sent.

Minimal Brevo template body:

```html
<img src="{{ params.logoUrl }}" alt="{{ params.clientName }}">
<h1>{{ params.copy.heading }}</h1>
<p>{{ params.copy.greeting }}</p>
<p>{{ params.copy.intro }}</p>
{% if params.code %}<p style="font-size:32px">{{ params.code }}</p>{% endif %}
{% for item in params.items %}
  <p><strong>{{ item.title }}</strong>{% if item.reference %} · {{ params.copy.labels.reference }} {{ item.reference }}{% endif %}</p>
  <a href="{{ item.cloudUrl }}" style="background:{{ params.buttonColour }}">{{ params.copy.cta }}</a>
{% endfor %}
<p>{{ params.copy.footer }}</p>
```

Set the template subject to `{{ params.copy.subject }}`. The API also sends it as `subject`, which overrides the template's own subject.

## Cloud ID links

Each email item links to `/cloud/{token}`.
- The token is 192 random bits, and only its SHA-256 is stored in `CloudLinks`.
- A link is bound to the sending tenant. Opened on another client's host, it returns 404.
- The summary shows product and cover only.
- Full details need a 6-digit code emailed to the link's address. The code lasts 10 minutes, allows 5 attempts, and at most 5 codes can be requested per hour. A correct code gives a 30-minute grant that the frontend keeps in an httpOnly cookie.
- To revoke a link, set `revoked: true` on its `CloudLinks` row.
