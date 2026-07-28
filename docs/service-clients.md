# ServiceClient credentials & auth tokens

How to create a `ServiceClient` document so that `POST /auth/token` issues a working JWT,
and how callers use that token against the rest of the API.

Nothing here changes application code — a token only works once a matching document
exists in the `ServiceClient` collection.

## 1. How the pieces fit together

| Piece | Where it lives |
| --- | --- |
| `ServiceClient` document (one per caller) | MongoDB, database `Activlink`, collection `ServiceClient` |
| Credential exchange endpoint | `POST /auth/token` (`routers/auth.py`) |
| Token issue/verify + hashing | `utils/jwt_auth.py` |
| Request-time verification | `verify_token` in `utils/dependencies.py` |
| Credential issuing CLI | `scripts/issue_service_client.py` |

Flow: caller posts `client_id` + `client_secret` → the API looks up the document, verifies the
secret against `secret_hash`, and returns a short-lived HS256 JWT → the caller sends that JWT as
`Authorization: Bearer <token>` on every other endpoint.

The secret is never stored in plaintext, so a `ServiceClient` document cannot be hand-written with
a raw secret — the `secret_hash` has to be generated (see §3 and §4).

## 2. Environment variables

Set these on the API service (Railway) before issuing tokens:

| Variable | Required | Notes |
| --- | --- | --- |
| `MONGO_URI` | yes | Without it `POST /auth/token` can never authenticate a client. |
| `JWT_SIGNING_SECRET` | yes | Strong random value, e.g. `openssl rand -hex 32`. Tokens cannot be issued or verified without it. |
| `JWT_ISSUER` | no | Defaults to `activlink-embedding`. Must be identical wherever tokens are issued and verified — verification checks the `iss` claim. |
| `ACCESS_TOKEN_EXPIRES_MINUTES` | no | Defaults to `45`. |
| `API_TOKEN` | legacy | The old single shared bearer token. |
| `ALLOW_LEGACY_API_TOKEN` | no | Defaults to `true`. Set to `false` once every caller uses ServiceClient tokens. |
| `ENFORCE_CLIENT_KEY_SCOPE` | no | Defaults to `true`. Set to `false` to log tenant-scope violations without rejecting them (see §7). |

Startup fails outright if neither `JWT_SIGNING_SECRET` nor `API_TOKEN` is set.

Changing `JWT_SIGNING_SECRET` or `JWT_ISSUER` invalidates every token already in circulation —
callers just re-request one, so the blast radius is at most `ACCESS_TOKEN_EXPIRES_MINUTES`.

## 3. Create the document (recommended)

Run the CLI from a machine that can reach Mongo, with `MONGO_URI` set:

```bash
export MONGO_URI="mongodb+srv://…/Activlink"
export JWT_SIGNING_SECRET="…"   # any non-empty value; the CLI only writes the client record

python scripts/issue_service_client.py \
  --client-id frontend \
  --scopes "*" \
  --created-by "paul"
```

It generates a random secret, stores only its hash, and prints the raw secret **once**:

```
ServiceClient created. Save this secret now — it cannot be retrieved again:
  client_id:     frontend
  client_secret: 8Kx…redacted…9tQ
  scopes:        *
```

Store the pair in the caller's secret store (Railway/Vercel env vars) as e.g.
`ACTIVLINK_CLIENT_ID` / `ACTIVLINK_CLIENT_SECRET`. If you lose the secret, issue a new
credential — there is no recovery path.

Options:

- `--client-id` — unique; a second run with the same id fails with `ServiceClient 'x' already exists`.
- `--scopes` — space-separated list. See §7 before choosing.
- `--client-key` — optional `ClientKey.ClientKey` this caller is scoped to; copied into the JWT's `client_key` claim.
- `--created-by` — free text, for audit.

## 4. Create the document manually

Only if you cannot run the CLI (e.g. you have Mongo Atlas UI access but no shell). Generate the
hash locally — it is PBKDF2-HMAC-SHA256, 260,000 iterations, stored as `salt$hexdigest` with the
hex salt used as the *literal* salt bytes:

```bash
python3 - <<'PY'
import hashlib, secrets, json, datetime
client_secret = secrets.token_urlsafe(32)
salt = secrets.token_hex(16)
h = hashlib.pbkdf2_hmac("sha256", client_secret.encode(), salt.encode(), 260_000)
print("client_secret (save this):", client_secret)
print(json.dumps({
    "client_id": "frontend",
    "secret_hash": f"{salt}${h.hex()}",
    "client_key": None,
    "scopes": ["*"],
    "active": True,
    "created_at": {"$date": datetime.datetime.utcnow().isoformat() + "Z"},
    "created_by": "paul",
}, indent=2))
PY
```

Insert the printed JSON into `Activlink.ServiceClient`, and make sure the unique index exists
(the app creates it on startup, but it is safe to create by hand):

```js
db.ServiceClient.createIndex({ client_id: 1 }, { unique: true })
```

### Field reference

| Field | Type | Meaning |
| --- | --- | --- |
| `client_id` | string | Caller identity. Unique. Becomes the JWT `sub`. |
| `secret_hash` | string | `salt$hexdigest`. Never the plaintext secret. |
| `client_key` | string \| null | Optional `ClientKey.ClientKey` this caller is pinned to; enforced per request (§7). `null` = may act on any client. |
| `scopes` | string[] | Copied into the JWT `scopes` claim. |
| `active` | bool | `false` rejects the credential at `/auth/token`. |
| `created_at` | date | Audit only. |
| `created_by` | string \| null | Audit only. |

A document missing `active: true` is treated as inactive and will fail authentication.

## 5. Get a token

```bash
curl -sS -X POST https://api.activlink.io/auth/token \
  -H "Content-Type: application/json" \
  -d '{"client_id":"frontend","client_secret":"…"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs…",
  "token_type": "bearer",
  "expires_in": 2700,
  "scopes": ["*"]
}
```

`/auth/token` deliberately has no bearer dependency — the `client_secret` in the body *is* the
credential being checked.

## 6. Use the token

```bash
curl -sS https://api.activlink.io/some-protected-route \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs…"
```

Cache the token in the calling service and refresh it before `expires_in` elapses (e.g. refresh at
80% of its lifetime) rather than exchanging credentials on every request. Never ship the
`client_secret` to a browser — exchange it server-side only.

## 7. client_id vs client_key, and tenant scoping

They answer different questions:

- **`client_id` — who is calling.** A name you invent for one piece of software holding a
  credential (`frontend`, `mcp-server`, `nightly-sku-import`). Must be unique; becomes the JWT `sub`.
- **`client_key` — whose data they may act on.** A real value from the `ClientKey` collection
  (`db.ClientKey.findOne({ClientKey: "AO12345"})`), used across the SKU routes to scope catalog
  data to one client. You do not invent it.

Naming a client_id after a tenant (`AO12345-widget`) is fine, but scoping comes from `client_key`
only — nothing resolves `client_id` against the `ClientKey` collection.

### Enforcement

`verify_token` holds a pinned caller to its tenant. If the credential has a non-null `client_key`,
every `ClientKey` value in the request — path params, query string, and JSON or form-encoded body,
including nested objects — must equal it, or the request is rejected:

```
403 {"detail": "Caller is scoped to ClientKey AO12345"}
```

Name matching ignores case and underscores, so `ClientKey`, `clientKey`, `client_key`, `clientkey`
and `Client_Key` are all covered. Body inspection mirrors FastAPI's own content-type rules, so it
sees every structured JSON media type (`application/merge-patch+json`, `application/vnd.api+json`,
…) and bodies sent with no `Content-Type` — anything the route itself would decode. Callers whose
`client_key` is `null` are unaffected (first-party callers, and the legacy static token).

Rolling this out to callers that already exist: set `ENFORCE_CLIENT_KEY_SCOPE=false` first. Every
violation is then logged as `[AUTH-SCOPE]` without a 403, so you can see which credentials are
reaching outside their tenant before enforcement starts returning errors.

### Row-level scoping

Rejecting requests that *name* another tenant is not enough on its own: an endpoint can reach
tenant data by a record id, a customer id, or no filter at all. So the contract, order and quote
routes also constrain their own queries via the `caller_client_key` dependency:

| Route | Behaviour for a pinned caller |
| --- | --- |
| `GET /contracts`, `GET /orders` | Filter forced to the caller's `client_key`, whatever the query string says. |
| `GET /customers/{id}/contracts`, `.../orders`, `GET /devices/{id}/contracts` | Same filter applied. |
| `GET /contracts/{id}`, `GET /orders/{id}`, `GET /quote/{id}` | `404` if the record belongs to another tenant. |
| `POST /contracts/{id}/cancel`, `.../resend` | `404` on another tenant's contract — mutations are scoped too. |
| `POST /contracts/jobs/*` | `403`. These sweep every tenant, so they are closed to pinned callers. |

Cross-tenant reads return **404, not 403**, so a pinned caller cannot use the status code to
discover that another tenant's record id exists.

Records whose owner field is empty or missing are invisible to a pinned caller. That matters for
orders: `contract_service.py` backfills an order's `client_key` from its first child contract, so
an unbackfilled order has `""` and will not appear. Quote documents store the owner as camelCase
`clientKey`, unlike contracts and orders.

Writing a new route that touches tenant-owned data? Take `scope: str | None = Depends(caller_client_key)`
and apply it to the query. The dependency alone will not cover you unless the request names a ClientKey.

### What this does not cover

`multipart/form-data` bodies are not inspected, since parsing them would consume file uploads. No
current route takes a ClientKey that way, but a new one would need its own check.

Routes outside contracts/orders/quotes that reach tenant data by a non-ClientKey identifier have
not been audited for this. The dependency still blocks any request that names another tenant.

### Scopes

`require_scope()` exists but no route uses it yet: every protected route depends on `verify_token`,
which only requires a valid token. The `scopes` list is recorded and carried in the JWT, but is not
enforced per route.

Practical consequence: pick meaningful scopes now (`sku:read`, `device:write`, `internal:*`) so
enforcement can be turned on later without re-issuing credentials. `"*"` matches everything once
`require_scope` starts being applied — use it only for trusted first-party callers.

## 8. Rotate or revoke

There is no rotation endpoint. To rotate, issue a new `client_id` (e.g. `frontend-2026-07`),
move the caller over, then deactivate the old one:

```js
db.ServiceClient.updateOne({ client_id: "frontend" }, { $set: { active: false } })
```

Deactivation blocks new token issuance immediately; already-issued JWTs stay valid until they
expire (at most `ACCESS_TOKEN_EXPIRES_MINUTES`). To kill outstanding tokens right now, rotate
`JWT_SIGNING_SECRET`.

## 9. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `401 Invalid client_id or client_secret` from `/auth/token` | No document for that `client_id`; `active` is not `true`; secret mismatch; or `MONGO_URI` unset on the API. | Check the document exists in `Activlink.ServiceClient` and that `active: true`. Re-issue if the secret was lost. |
| Same 401, and the secret you sent looks like `<hex>$<hex>` | `secret_hash` was sent as the `client_secret`. The hash is the stored form; it is not a credential. | Send the plaintext secret printed at issue time. If it is lost, issue a new credential (§3). |
| `500` from `/auth/token` with `JWT_SIGNING_SECRET not configured` | Credentials verified but no signing secret on the service. | Set `JWT_SIGNING_SECRET` and redeploy. |
| `401 Invalid or missing token` on other routes | Expired token; token signed with a different `JWT_SIGNING_SECRET`; `iss` does not match `JWT_ISSUER`; or the header is not `Authorization: Bearer <token>`. | Re-request a token; confirm both env vars match across environments. |
| `403 Caller is scoped to ClientKey x` | A pinned caller asked for another client's data. | Use the right ClientKey, or issue a credential with `client_key: null` if the caller legitimately spans clients (§7). |
| `403 Missing required scope: x` | The caller's `scopes` lack that scope. | Issue a credential with the right scopes (§7). |
| `ServiceClient 'x' already exists` | `client_id` collides with the unique index. | Pick a different id, or deactivate/delete the old document first. |
| `[jwt_auth] Could not create ServiceClient index: …` in logs | The Mongo user cannot create indexes. | Create the unique index manually (§4). |
| Legacy `[AUTH-DEPRECATED]` warnings in logs | A caller is still using the shared `API_TOKEN`. | Migrate it (§10). |

## 10. Migrating off the legacy shared token

1. Issue a `ServiceClient` per caller (§3).
2. Point each caller at `/auth/token` and have it send the resulting JWT.
3. Watch the logs until no `[AUTH-DEPRECATED]` warnings appear.
4. Set `ALLOW_LEGACY_API_TOKEN=false`, then unset `API_TOKEN`.
