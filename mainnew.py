# main.py

import asyncio
from fastapi import FastAPI

# Import routers (grouped for clarity)
from routers import (
    embedded_register_device,
    match,
    categories,
    client_lookup,
    lookup_locale_params,
    ai_extract_and_match,
    product_assignment,
    rate_request,
    generate_payment_link,
    sync_stripe_prices,
    generate_faults,
    vision,
    sms,
    create_customer,
    device_register,
    assign_product_by_device_id,
    assign_device_collection,
    generate_payment_links_from_quote,
    props_lookup,
    stripe_webook,
)
from routers.enrich import (
    ice_lookup,
    go_upc,
    scale_lookup,
)

from routers.sku import (
    lookup_custom_sku,
    lookup_master_sku,
    lookup_custom_sku_all,
    create_custom_sku,
    lookup_master_sku_all,
    create_master_sku,
)

# Try to import poll_imap
#try:
#    from routers.email_injest import poll_imap
#    print("[DEBUG] poll_imap imported successfully")
#except Exception as e:
#    print(f"[DEBUG] Could not import poll_imap: {e}")
#    poll_imap = None


# Initialize FastAPI app
app = FastAPI(
    title="Activlink API Suite",
    description="Match natural language queries to device categories using OpenAI embeddings.",
    version="1.0.0",
)

print("[DEBUG] FastAPI app object created")

# Include all route modules
app.include_router(match.router)
app.include_router(categories.router)
app.include_router(client_lookup.router)
app.include_router(lookup_locale_params.router)
app.include_router(lookup_custom_sku.router)
app.include_router(lookup_custom_sku_all.router)
app.include_router(create_custom_sku.router)
app.include_router(lookup_master_sku.router)
app.include_router(lookup_master_sku_all.router)
app.include_router(create_master_sku.router)
app.include_router(ice_lookup.router)
app.include_router(go_upc.router)
app.include_router(scale_lookup.router)
app.include_router(ai_extract_and_match.router)
app.include_router(embedded_register_device.router)
app.include_router(product_assignment.router)
app.include_router(rate_request.router)
app.include_router(generate_payment_link.router)
app.include_router(sync_stripe_prices.router)
app.include_router(generate_faults.router)
app.include_router(vision.router)
app.include_router(sms.router)
app.include_router(create_customer.router)
app.include_router(device_register.router)
app.include_router(assign_product_by_device_id.router)
app.include_router(assign_device_collection.router)
app.include_router(generate_payment_links_from_quote.router)
app.include_router(props_lookup.router)
app.include_router(stripe_webook.router)

print("[DEBUG] Routers registered")


# Background task: poll inbox every 20 seconds
#@app.on_event("startup")
#async def start_email_polling():
#    print("[DEBUG] Startup event triggered")
#    if poll_imap is None:
#        print("[DEBUG] poll_imap not available, skipping poller")
#        return

#    async def poll_task():
#        print("[DEBUG] Poll task created")
#        while True:
#            try:
#                print("[DEBUG] Poll loop iteration starting")
#                await poll_imap(limit=1)  # keep it small for debug
#                print("[DEBUG] poll_imap finished one run")
#            except Exception as e:
#                print(f"[DEBUG] Poll task error: {e}")
#            await asyncio.sleep(10)  # shorter delay while debugging

#    asyncio.create_task(poll_task())
#    print("[DEBUG] Startup event finished (poll_task scheduled)")
