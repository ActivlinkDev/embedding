# Contract Administration

System of record for **device-cover contracts**, living inside the Activlink API
Suite (`C:\embedding`). Contracts are issued automatically when a Stripe payment
completes and are then administered over their lifetime.

MongoDB-backed (`Activlink` db), bearer-authed, registered through the standard
`main.py` router-include pattern. No separate service or database — it ships with the
existing single Railway deploy and reuses the existing `/stripe/webhook`.

## Files
| File | Purpose |
|---|---|
| `contract_service.py` | Core service: `Contracts` collection, `create_contract()`, lifecycle, references, audit trail |
| `admin.py` | Read/manage endpoints (Contracts tag, `verify_token` auth) |

## How issuance works
The existing `routers/stripe_webook.py` calls `create_contract(session, customer_id)`
on `checkout.session.completed`, after the customer is created/paired. The **basket is
the source of truth**: it's loaded from `Basket_Quotes` by the `basket_id` in the
session metadata, and the per-device fields come from each basket item — we do **not**
rely on the frontend stamping per-device Stripe metadata.

Issuance creates a **two-level hierarchy** (one Stripe charge -> many per-device
policies):
* a parent **`ContractOrders`** doc (one per checkout session) holding the *verified
  Stripe `amount_total`*, and
* one child **`Contracts`** doc per basket item, each with its own device, offer, and
  item price.

Idempotent: parent on `stripe_session_id`, children on a per-item `dedupe_key`.
One-off checkouts that arrive already paid activate immediately; monthly cover
activates on the first paid invoice (see "Subscriptions" below).

## Amounts (important)
* **Parent order** stores `stripe_amount_total` = the actual amount Stripe charged
  (verified, from the event). This is the financial source of truth.
* **Each child contract** stores `price` = that basket item's `rounded_price`.
* On `_refresh_order`, the sum of child prices (`items_price_sum`) is compared to
  `stripe_amount_total` and `amount_reconciled` is set true/false — so a basket
  discount or rounding mismatch is visible rather than silent.

## Data model — `ContractOrders` (parent)
```jsonc
{
  "_id": ObjectId,
  "order_reference": "ORD-2026-000123",
  "basket_id": "<Basket_Quotes _id str>",
  "customer_id": "<customer ObjectId str>",
  "client_key": "Beko",
  "status": "PENDING_ACTIVATION | ACTIVE | EXPIRED | CANCELLED",  // rolled up from children
  "billing": "ONE_OFF | MONTHLY",
  "stripe_session_id": "cs_...",          // idempotency key (unique)
  "stripe_payment_intent_id": "pi_...",
  "stripe_subscription_id": "sub_... | null",
  "stripe_customer_id": "cus_...",
  "stripe_amount_total": 146.97,          // VERIFIED Stripe total
  "currency": "GBP",
  "item_count": 3,
  "items_price_sum": 146.97,              // sum of child prices
  "amount_reconciled": true,              // items_price_sum == stripe_amount_total?
  "created_at": ISODate, "updated_at": ISODate
}
```

## Data model — `Contracts` (child, one per device)
```jsonc
{
  "_id": ObjectId,
  "reference": "ACT-2026-000123",   // atomic, via Counters collection
  "order_id": ObjectId,             // -> ContractOrders._id
  "order_reference": "ORD-2026-000123",
  "customer_id": "<customer ObjectId str>",
  "device_id": "DEV-...",           // from basket item deviceId
  "offer_id": "EX1",                // from basket item product_id
  "quote_id": "Q-...", "basket_id": "BSK-...",
  "client_key": "Beko", "source": "POS",
  "status": "PENDING_ACTIVATION | ACTIVE | EXPIRED | CANCELLED | RENEWED | VOID",
  "cover_type": "Dishwasher",       // from basket item category
  "term_months": 12,
  "start_date": ISODate, "end_date": ISODate,
  "billing": "ONE_OFF | MONTHLY",
  "price": 48.99, "currency": "GBP", "auto_renew": false,
  "stripe_customer_id": "cus_...",
  "stripe_session_id": "cs_...",
  "stripe_payment_intent_id": "pi_...",
  "stripe_subscription_id": "sub_... | null",
  "dedupe_key": "cs_...::Q-...",    // per-item idempotency key (unique)
  "document_url": null, "ipid_url": null,
  "events":  [ { "type", "payload", "actor", "at" } ],   // append-only audit
  "payments":[ { "stripe_event_id", "stripe_object_id", "kind", "amount",
                 "currency", "status", "at" } ],
  "created_at": ISODate, "updated_at": ISODate,
  "cancelled_at": null, "cancel_reason": null
}
```
A lightweight `contract_refs[]` back-reference is also kept on the customer document.
The parent order's `status` is **rolled up** from its children whenever any child
transitions (ACTIVE if any active, CANCELLED if all cancelled/void, EXPIRED if all
expired).

## Lifecycle
```
PENDING_ACTIVATION ─(paid)─▶ ACTIVE ─(end_date)─▶ EXPIRED
        │                      │ ▲
   (refund pre-               │ └─(sub invoice / auto_renew)─ RENEWED → new ACTIVE
    activation)                └─(refund / request / sub cancel)─▶ CANCELLED
        ▼
      VOID
```
Transitions are enforced in `contract_service._apply`; every transition appends an
event, so `events[]` is the legal audit trail.

## Endpoints (all require the bearer token)
| Method | Path | Purpose |
|---|---|---|
| GET  | `/orders` | list parent orders: `customer_id, client_key, status, limit` |
| GET  | `/orders/{id}` | order (Stripe total + reconciliation) **with its child contracts** |
| GET  | `/customers/{customer_id}/orders` | a customer's orders |
| GET  | `/contracts` | list/filter: `customer_id, device_id, client_key, status, expiring_within_days, limit` |
| GET  | `/contracts/{id}` | full child contract (events + payments) |
| POST | `/contracts/{id}/cancel` | body `{reason, refund?}` — cancels ACTIVE / voids PENDING (rolls up to order) |
| POST | `/contracts/{id}/resend` | re-send policy docs (stubbed → records NOTIFIED event) |
| GET  | `/customers/{customer_id}/contracts` | powers the customer-hub "Contracts" tab |
| GET  | `/devices/{device_id}/contracts` | device cover history |

## Required checkout metadata
Only **`basket_id`** is required on the Stripe **Checkout Session** `metadata` — the
basket document supplies everything else (already set in `routers/basket/payment.py`).
`billing` is derived from the session mode/subscription, not metadata. Per-item term is
derived by `_item_term_months` (extend that mapping as products gain explicit terms;
default 12 months).

## Indexes (created on the live DB; re-run is idempotent)
```js
// children
db.Contracts.createIndex({ dedupe_key: 1 }, { unique: true, sparse: true })
db.Contracts.createIndex({ order_id: 1 })
db.Contracts.createIndex({ customer_id: 1, created_at: -1 })
db.Contracts.createIndex({ device_id: 1 })
db.Contracts.createIndex({ stripe_subscription_id: 1 })
// parent
db.ContractOrders.createIndex({ stripe_session_id: 1 }, { unique: true, sparse: true })
db.ContractOrders.createIndex({ customer_id: 1, created_at: -1 })
db.ContractOrders.createIndex({ basket_id: 1 })
```

## Scheduled jobs
This app has no in-process scheduler, so the jobs are exposed as token-protected
endpoints for **Railway Cron** (or any scheduler) to hit daily:
- `POST /contracts/jobs/expiry-check` → `expiry_checker()`: ACTIVE → EXPIRED past
  `end_date`, rolls up affected orders.
- `POST /contracts/jobs/renewal-notify?notice_days=30` → `renewal_notifier()`: records
  a `NOTIFIED` event for contracts within the window.

Set up on Railway: add a **Cron** service in the project with a schedule (e.g.
`0 2 * * *`) that curls the endpoint with the bearer token.

## Subscriptions (monthly) — implemented
`routers/stripe_webook.py` dispatches:
- `invoice.paid` (`subscription_create`) → activate the pending contract
- `invoice.paid` (`subscription_cycle`) → record renewal + spawn next-term contract
- `charge.refunded` → void (pending) or cancel (active)
- `customer.subscription.deleted` → cancel the active contract

(See `handle_invoice_paid`, `handle_charge_refunded`, `handle_subscription_deleted`.)

## Not yet implemented (stubs in place)
- **Policy PDF / IPID generation** → `document_url` / `ipid_url` stay null until wired.
  Hook point: call a generator at the end of `activate()` and store the URLs.
- **Email sending** → `/resend` and `renewal_notifier` record `NOTIFIED` events only.
  Hook point: a `notifications.py` using the existing `BREVO_API_KEY` env var, called
  from `activate()` (policy docs) and `renewal_notifier()` (renewal notice).
