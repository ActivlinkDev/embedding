# main.py

from fastapi import FastAPI

# Import routers (grouped for clarity)
from routers import (
    embedded_register_device,
    match,
    categories,
    client_lookup,
    lookup_custom_sku,
    lookup_master_sku,
    lookup_locale_params,
    lookup_custom_sku_all,
    create_custom_sku,
    lookup_master_sku_all,
    ice_lookup,
    go_upc,
    scale_lookup,
    ai_extract_and_match,
    create_master_sku,
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
)

# Initialize FastAPI app
app = FastAPI(
    title="Activlink API Suite",
    description="Match natural language queries to device categories using OpenAI embeddings.",
    version="1.0.0",
)

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
