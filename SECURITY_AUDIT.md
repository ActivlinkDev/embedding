# Security Audit Notes

Date: 2026-06-15

## Scope

Reviewed the FastAPI application, router registration, bearer-token dependency, OTP cookie signing, CMS proxying, and AI extraction paths. Attempted to run automated dependency/static-analysis tooling, but the package index blocked installation in this environment.

## Remediated in this branch

- Disabled interactive API documentation and the OpenAPI schema by default. Set `ENABLE_API_DOCS=true` only in trusted non-production environments.
- Added common HTTP hardening headers to every response.
- Switched bearer-token comparison to constant-time comparison.
- Removed unsafe `eval()` parsing of model output and replaced it with strict JSON parsing.
- Added bearer-token protection to client lookup and Strapi proxy endpoints.
- Restricted Strapi proxy routes to relative paths and blocked traversal/absolute URL patterns.
- Removed the weak fallback OTP cookie-signing secret; deployments must set `OTP_COOKIE_SECRET` or `LOOKUP_API_KEY`.
- Switched signed OTP fallback cookie verification to constant-time signature comparison.

## Remaining enterprise-readiness recommendations

1. Add centralized rate limiting for login, OTP, payment-link, AI, and enrichment endpoints.
2. Replace single shared bearer token auth with scoped service credentials or OAuth2/JWT validation, key rotation, and audit logging.
3. Add dependency vulnerability scanning in CI, for example `pip-audit` or GitHub Dependabot.
4. Add static analysis in CI, for example Bandit/Semgrep, and fail builds on high-severity findings.
5. Add structured security logging without sensitive payloads, tokens, customer PII, or payment identifiers.
6. Validate all externally supplied identifiers with allowlisted formats before using them in MongoDB queries.
7. Put admin/operations endpoints behind separate network controls or stricter authorization.
8. Add production CORS policy explicitly if browsers call this API directly; avoid wildcard origins with credentials.
9. Configure secret management outside `.env` files in production and rotate any secrets that may have been logged.
10. Add integration tests that assert protected endpoints reject missing/invalid credentials.

## Commands attempted

- `python -m pip install --quiet bandit pip-audit detect-secrets && bandit -q -r . -x './.git,./venv,./.venv' -f json -o /tmp/bandit.json; python -m json.tool /tmp/bandit.json | head -200; pip-audit -r requirements.txt || true; detect-secrets scan --all-files --exclude-files '\.git|\.old$|/venv|/\.venv' > /tmp/secrets.json && python -m json.tool /tmp/secrets.json | head -200`
  - Result: failed because package installation from the package index returned `403 Forbidden`.
- `python -m py_compile main.py utils/dependencies.py routers/ai_extract_and_match.py routers/client_lookup.py routers/cms/strapi.py routers/otp.py routers/customer/mark_verified.py`
  - Result: passed.
- `API_TOKEN=test OPENAI_API_KEY=test OTP_COOKIE_SECRET='0123456789abcdef0123456789abcdef' STRIPE_WEBHOOK_SECRET=whsec_test python - <<'PY' ...`
  - Result: could not run because `fastapi` is not installed in the environment.
- `pytest -q`
  - Result: could not run because required dependencies such as `pymongo` are not installed in the environment.
