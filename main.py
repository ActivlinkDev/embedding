# main.py

from fastapi import FastAPI
from routers import match, categories, client_lookup, lookup_custom_sku,lookup_master_sku

app = FastAPI(
    title="Activlink API Suite",
    description="Match natural language queries to device categories using OpenAI embeddings.",
    version="1.0.0"
)

# Include route modules
app.include_router(match.router)
app.include_router(categories.router)
app.include_router(client_lookup.router)
app.include_router(lookup_custom_sku.router) 
app.include_router(lookup_master_sku.router) 
