import os
import json
import numpy as np
import openai
import logging
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenAI Key
openai.api_key = os.getenv("OPENAI_API_KEY")
if not openai.api_key:
    logger.error("OPENAI_API_KEY not set!")
    raise ValueError("OPENAI_API_KEY not set!")

# Load category list
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

# Load embeddings
try:
    category_embeddings = np.load("category_embeddings.npz")["embeddings"].tolist()
    logger.info(f"Loaded {len(category_embeddings)} category embeddings.")
except Exception as e:
    logger.error(f"Error loading embeddings: {e}")
    raise

# Utilities
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
