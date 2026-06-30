# main.py — safe startup with health check and skippable routers
import os
import importlib
import asyncio, traceback
from fastapi import FastAPI, Request
from routers.quote import router as quote_router

OPENAPI_TAGS = [
    {"name": "Catalog", "description": "Category, SKU, and client catalog lookups."},
    {"name": "Localization", "description": "Locale lookups and mappings."},
    {"name": "Enrichment", "description": "Third-party enrichment and AI extraction."},
    {"name": "Devices", "description": "Device registration and lookup."},
    {"name": "Assignments", "description": "Device and product assignment workflows."},
    {"name": "Quotes", "description": "Quote generation and retrieval."},
    {"name": "Payments", "description": "Rates, payment links, and Stripe utilities."},
    {"name": "Contracts", "description": "Device-cover contract issuance and administration."},
    {"name": "Basket", "description": "Basket pricing and payments."},
    {"name": "Customers", "description": "Customer identity and verification."},
    {"name": "Messaging", "description": "SMS, email ingest, and OTP flows."},
    {"name": "CMS", "description": "Content management integrations."},
    {"name": "Operations", "description": "Internal tools and maintenance utilities."},
    {"name": "QA", "description": "Quality assurance utilities."},
    {"name": "Portal", "description": "Portal admin login and user management."},
    {"name": "QR", "description": "QR code collection generation, scanning, and pairing."},
]

docs_enabled = os.getenv("ENABLE_API_DOCS", "false").lower() == "true"

app = FastAPI(
    title="Activlink API Suite",
    description="APIs for registration, ingestion, enrichment, and payments.",
    version="1.0.0",
    openapi_tags=OPENAPI_TAGS,
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# -------- Health check --------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

def _include_router(module_path: str, attr: str = "router") -> None:
    """Import a router module safely and include its FastAPI router."""
    try:
        mod = importlib.import_module(module_path)
    except Exception as e:
        print(f"[ROUTER-IMPORT] FAILED to import '{module_path}': {e}")
        return
    try:
        router = getattr(mod, attr)
    except Exception as e:
        print(f"[ROUTER-IMPORT] Module '{module_path}' missing '{attr}': {e}")
        return
    try:
        app.include_router(router)
        print(f"[ROUTER-IMPORT] Included '{module_path}'")
    except Exception as e:
        print(f"[ROUTER-IMPORT] FAILED to include router from '{module_path}': {e}")

# -------- Which routers to include? --------
skip = {
    s.strip()
    for s in (os.getenv("SKIP_ROUTERS") or "").split(",")
    if s.strip()
}

ROUTERS = {
    # core/product/sku
    "match": "routers.match",
    "categories": "routers.categories",
    "client_lookup": "routers.client_lookup",
    "lookup_locale_params": "routers.lookup_locale_params",
    "lookup_custom_sku": "routers.sku.lookup_custom_sku",
    "lookup_custom_sku_all": "routers.sku.lookup_custom_sku_all",
    "lookup_custom_sku_locale": "routers.sku.lookup_custom_sku_locale",
    "create_custom_sku": "routers.sku.create_custom_sku",
    "update_custom_sku": "routers.sku.update_custom_sku",
    "get_custom_sku": "routers.sku.get_custom_sku",
    "delete_custom_sku": "routers.sku.delete_custom_sku",
    "lookup_master_sku": "routers.sku.lookup_master_sku",
    "lookup_master_sku_all": "routers.sku.lookup_master_sku_all",
    "create_master_sku": "routers.sku.create_master_sku",
    "quick_search": "routers.sku.quick_search",

    # enrich
    "ice_lookup": "routers.enrich.ice_lookup",
    "ice_brand_index": "routers.enrich.ice_brand_index",
    "go_upc": "routers.enrich.go_upc",
    "dseo_shopping": "routers.enrich.dseo_shopping",
    "dseo_product_info": "routers.enrich.dseo_product_info",
    "dseo_webhook": "routers.enrich.dseo_webhook",

    # registration / assignment
    "embedded_register_device": "routers.embedded_register_device",
    "device_register": "routers.device_register",
    "devices": "routers.devices.get_device_by_id",
    "assign_product_by_device_id": "routers.assign_product_by_device_id",
    "assign_device_collection": "routers.assign_device_collection",
    "embedded_quote": "routers.embedded_quote",
    "product_assignment": "routers.product_assignment",

    # payments / pricing
    "rate_request": "routers.rate_request",
    "generate_payment_link": "routers.generate_payment_link",
    "sync_stripe_prices": "routers.sync_stripe_prices",
    "stripe_webook": "routers.stripe_webook",
    "contract_admin": "routers.contract.admin",
    # basket
    "basket": "routers.basket",
    "ratebasket": "routers.basket.ratebasket",
    "basket_payment": "routers.basket.payment",

    # misc features
    "generate_faults": "routers.generate_faults",
    "vision": "routers.vision",
    "sms": "routers.sms",
    "create_customer": "routers.customer.create_customer",
    "pair_customer": "routers.customer.pair_customer",
    "get_customer_by_id": "routers.customer.get_by_id",
    "authenticate_customer": "routers.customer.authenticate_customer",
    "mark_verified": "routers.customer.mark_verified",
    "generate_payment_links_from_quote": "routers.generate_payment_links_from_quote",
    "locale_infos": "routers.locale_infos",
    "locales": "routers.locales",
    "otp": "routers.otp",

    # cms
    "props_lookup": "routers.cms.props_lookup",
    "cms_display_offer": "routers.cms.cms_display_offer",
    "strapi_proxy": "routers.cms.strapi",
    "validate_customer": "routers.cms.validate_customer",

    # qr
    "generate_qr_collection": "routers.qr.generate_qr_collection",
    "generate_device_qr":     "routers.qr.generate_device_qr",
    "scan_qr":                "routers.qr.scan_qr",
    "pair_qr_to_device":      "routers.qr.pair_qr_to_device",
    "get_qr":                 "routers.qr.get_qr",
    "list_qr_collection":     "routers.qr.list_qr_collection",

    # portal admin
    "portal": "routers.portal",

    # email ingest 
    "email_ingest": "routers.email_ingest",
     # QA
    "qa": "routers.qa",
}

print(f"[STARTUP] SKIP_ROUTERS={sorted(skip)}")

for name, module_path in ROUTERS.items():
    if name in skip:
        print(f"[ROUTER-IMPORT] Skipping '{name}' ({module_path}) per SKIP_ROUTERS")
        continue
    _include_router(module_path)
app.include_router(quote_router)

# -------- Background poller (multi-mailbox) --------
if os.getenv("ENABLE_EMAIL_POLL", "false").lower() == "true":
    try:
        from routers.email_ingest import poll_mailbox, MAILBOXES

        async def _poll_loop():
            print("[EMAIL-POLLER] Starting background poll loop (20s)")
            while True:
                for config in MAILBOXES:
                    try:
                        await poll_mailbox(config, limit=2)
                    except Exception as e:
                        print(f"[EMAIL-POLLER][{config.get('id')}] Error: {e}")
                        traceback.print_exc()
                await asyncio.sleep(20)

        @app.on_event("startup")
        async def _start_poller():
            print("[EMAIL-POLLER] Scheduling background task")
            asyncio.create_task(_poll_loop())

    except Exception as e:
        print(f"[EMAIL-POLLER] Not enabled or failed to import: {e}")
