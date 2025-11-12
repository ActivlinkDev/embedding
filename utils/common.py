import os
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

# Precomputed embeddings file removed: this module no longer depends on a
# local "generate_embeddings.npz" file. If you previously relied on
# precomputed embeddings, provide them at runtime to functions that need
# them (for example via a database or by passing arrays to helpers).
category_embeddings = []
device_categories = []

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
