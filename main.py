# main.py

from fastapi import FastAPI
from routers import match, categories, client_lookup, lookup_custom_sku,lookup_master_sku,lookup_locale_params,lookup_custom_sku_all,create_custom_sku,lookup_master_sku_all,ice_lookup,go_upc,scale_lookup,ai_extract_and_match,create_master_sku,register_device

app = FastAPI(
    title="Activlink API Suite",
    description="Match natural language queries to device categories using OpenAI embeddings.",
    version="1.0.0"
)

# Include route modules
app.include_router(match.router)
app.include_router(categories.router)
app.include_router(lookup_locale_params.router)
app.include_router(client_lookup.router)
app.include_router(lookup_custom_sku.router) 
app.include_router(lookup_custom_sku_all.router) 
app.include_router(create_custom_sku.router) 
app.include_router(lookup_master_sku_all.router)
app.include_router(lookup_master_sku.router)
app.include_router(create_master_sku.router)
app.include_router(ice_lookup.router)
app.include_router(go_upc.router)
app.include_router(scale_lookup.router)
app.include_router(ai_extract_and_match.router)
app.include_router(register_device.router)