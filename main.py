from fastapi import FastAPI
from routers import match, categories

app = FastAPI(
    title="Device Category Matcher API",
    description="Match natural language queries to device categories using OpenAI embeddings.",
    version="1.0.0"
)

# Include route modules
app.include_router(match.router)
app.include_router(categories.router)
