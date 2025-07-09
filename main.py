import os
import json
import logging
import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel
import openai

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set up OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")
if not openai.api_key:
    logger.error("OPENAI_API_KEY environment variable not set!")
    raise ValueError("OPENAI_API_KEY environment variable not set!")

app = FastAPI()

# Load device categories from local file
def fetch_device_categories_local(filepath="category.json"):
    logger.info(f"Loading device categories from {filepath}...")
    try:
        with open(filepath, "r") as f:
            categories = json.load(f)
        logger.info(f"Loaded {len(categories)} categories.")
        return categories
    except Exception as e:
        logger.error(f"Error loading category file: {e}")
        raise

device_categories = fetch_device_categories_local()

# Generate embeddings
def embed_texts(texts):
    logger.info("Generating category embeddings...")
    response = openai.embeddings.create(
        model="text-embedding-3-large",
        input=texts
    )
    embeddings = [item.embedding for item in response.data]
    logger.info("Category embeddings generated.")
    return embeddings

category_embeddings = embed_texts(device_categories)

def embed_query(query: str):
    response = openai.embeddings.create(
        model="text-embedding-3-large",
        input=query
    )
    return response.data[0].embedding

def cosine_similarity(a, b):
    a = np.array(a)
    b = np.array(b)
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

def find_best_match(query_embedding, category_embeddings, categories):
    similarities = [cosine_similarity(query_embedding, emb) for emb in category_embeddings]
    best_idx = int(np.argmax(similarities))
    return categories[best_idx], float(similarities[best_idx])

# Request/response models
class QueryRequest(BaseModel):
    query: str

class MatchResponse(BaseModel):
    category: str
    similarity: float

# Endpoint
@app.post("/match", response_model=MatchResponse)
def match_category(request: QueryRequest):
    query_embedding = embed_query(request.query)
    category, similarity = find_best_match(query_embedding, category_embeddings, device_categories)
    return MatchResponse(category=category, similarity=similarity)
