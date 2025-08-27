# main.py — safe startup with health check and skippable routers
import os
import importlib
import asyncio, traceback
from fastapi import FastAPI

app = FastAPI(
    title="Activlink API Suite",
    description="APIs for registration, ingestion, enrichment, and payments.",
    version="1.0.0",
)

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
    "create_custom_sku": "routers.sku.create_custom_sku",
    "lookup_master_sku": "routers.sku.lookup_master_sku",
    "lookup_master_sku_all": "routers.sku.lookup_master_sku_all",
    "create_master_sku": "routers.sku.create_master_sku",

    # enrich
    "ice_lookup": "routers.enrich.ice_lookup",
    "go_upc": "routers.enrich.go_upc",
    "scale_lookup": "routers.enrich.scale_lookup",

    # registration / assignment
    "embedded_register_device": "routers.embedded_register_device",
    "device_register": "routers.device_register",
    "assign_product_by_device_id": "routers.assign_product_by_device_id",
    "assign_device_collection": "routers.assign_device_collection",
    "product_assignment": "routers.product_assignment",

    # payments / pricing
    "rate_request": "routers.rate_request",
    "generate_payment_link": "routers.generate_payment_link",
    "sync_stripe_prices": "routers.sync_stripe_prices",
    "stripe_webook": "routers.stripe_webook",

    # misc features
    "generate_faults": "routers.generate_faults",
    "vision": "routers.vision",
    "sms": "routers.sms",
    "create_customer": "routers.create_customer",
    "generate_payment_links_from_quote": "routers.generate_payment_links_from_quote",
    "props_lookup": "routers.props_lookup",

    # email ingest 
    "email_ingest": "routers.email_ingest",
}

print(f"[STARTUP] SKIP_ROUTERS={sorted(skip)}")

for name, module_path in ROUTERS.items():
    if name in skip:
        print(f"[ROUTER-IMPORT] Skipping '{name}' ({module_path}) per SKIP_ROUTERS")
        continue
    _include_router(module_path)

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
