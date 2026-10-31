# Confirmation emails (Brevo) and email verification

| Email | Sent when | Hook | Dedupe key |
|---|---|---|---|
| Plan confirmation | Stripe `checkout.session.completed` has issued contracts | `routers/stripe_webook.py`, after `create_contract` | `plan:{order_reference}` |
| Registration confirmation (web) | Devices are paired to a customer on a journey with no purchase | `routers/customer/pair_customer_devices.py` | `registration:{customer_id}:{device-set hash}` |
| Registration confirmation (embedded) | `POST /register` stored at least one new device for a customer with contact details | `routers/embedded_register_device.py` | `registration:{customer_id}:{device-set hash}` |
| Verification code | Customer chooses "email me a code" on /my-registrations or /my-contracts | `POST /otp/email/request` (`routers/email_otp.py`) | per send |

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
| `EMAIL_OTP_SECRET` | 32+ characters. Hashes email verification codes. Without it the email-code router is not loaded, and /my-registrations and /my-contracts offer SMS only. |
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
      "linkUrl": "https://ao.registermyproduct.io/my-registrations?link=<token>&locale=en_GB" }
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
  <a href="{{ item.linkUrl }}" style="background:{{ params.buttonColour }}">{{ params.copy.cta }}</a>
{% endfor %}
<p>{{ params.copy.footer }}</p>
```

Set the template subject to `{{ params.copy.subject }}`. The API also sends it as `subject`, which overrides the template's own subject.

## Links and verification

Each registration item links to `/my-registrations?link=<token>`, each plan item to `/my-contracts?link=<token>` (`links.py`).

- **The token grants nothing.** Those pages still need an SMS or email code.
- Before verification, the token lets the page send the code to the address the email went to, without the customer typing it.
- After verification, it highlights the linked record, but only when that record is already in the verified customer's results for this client.
- The token is 192 random bits, and only its SHA-256 is stored, in `EmailLinks`. A link is bound to the sending client; on another client's host it resolves to nothing.
- To disable a link, set `revoked: true` on its row.

Email codes (`otp_codes.py`, collection `EmailOtp`):
- A code is only sent when the address belongs to a customer with devices or contracts on this client. The response is identical either way, and unregistered addresses count towards the same limits.
- Codes are 6 digits, last 10 minutes, and allow 5 attempts. At most 5 can be requested per address per hour.
- Once verified, `/my-registrations` and `/my-contracts` look records up through every `Customer` with that email (case-insensitive exact match).
