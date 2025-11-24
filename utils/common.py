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
    """Return the best matching category and its similarity.

    Defensive: if `category_embeddings` is empty or similarities cannot be
    computed, return (None, 0.0) instead of raising an exception.
    """
    if not category_embeddings:
        logger.warning("find_best_match called with empty category_embeddings")
        return None, 0.0

    try:
        similarities = np.array([
            cosine_similarity(query_embedding, emb) for emb in category_embeddings
        ], dtype=float)
    except Exception:
        logger.exception("Error computing similarities in find_best_match")
        return None, 0.0

    # If there are no computed similarities or all are NaN, return default
    if similarities.size == 0 or np.all(np.isnan(similarities)):
        logger.warning("No valid similarities computed (empty or all-NaN)")
        return None, 0.0

    # Treat NaNs as very small so argmax ignores them
    nan_mask = np.isnan(similarities)
    if np.any(nan_mask):
        similarities[nan_mask] = -np.inf

    best_idx = int(np.nanargmax(similarities))
    best_similarity = float(similarities[best_idx])

    if not categories or best_idx < 0 or best_idx >= len(categories):
        logger.warning("find_best_match computed index out of range for categories")
        return None, best_similarity

    return categories[best_idx], best_similarity


def mongo_vector_search(query_embedding, mongo_uri: str = None, db_name: str = "Activlink", collection_name: str = "Category", index: str = None, num_candidates: int = 100):
    """Try to find the best category using MongoDB vectorSearch (if available).

    Returns a tuple (matched_category_or_None, similarity_float). This is
    best-effort: any error or missing configuration results in (None, 0.0).
    """
    if index is None:
        index = os.getenv("VECTOR_INDEX", "vector_index")

    if not mongo_uri:
        mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        logger.debug("mongo_vector_search: no MONGO_URI configured")
        return None, 0.0

    try:
        from pymongo import MongoClient
    except Exception:
        logger.debug("mongo_vector_search: pymongo not available")
        return None, 0.0

    try:
        client = MongoClient(mongo_uri)
        db = client[db_name]
        coll = db[collection_name]

        # ensure query vector is a plain list of floats
        try:
            qvec = list(query_embedding)
        except Exception:
            qvec = [float(x) for x in query_embedding]

        stage = {
            "$vectorSearch": {
                "index": index,
                "path": "embedding",
                "queryVector": qvec,
                "numCandidates": int(num_candidates),
                "limit": 1,
            }
        }

        results = list(coll.aggregate([stage]))
        if not results:
            return None, 0.0

        doc = results[0]
        # category field may have different names in documents
        cat = doc.get("category") or doc.get("Category") or doc.get("category_name")

        # Extract score if provided by server
        score = None
        for k in ("score", "searchScore", "vectorSearchScore", "scoreValue", "_score"):
            if k in doc:
                try:
                    score = float(doc[k])
                except Exception:
                    score = None
                break

        # If no score provided but embedding present, compute cosine locally
        if score is None and isinstance(doc.get("embedding"), (list, tuple)):
            try:
                score = float(cosine_similarity(query_embedding, doc["embedding"]))
            except Exception:
                score = None

        return (cat, float(score) if score is not None else 0.0)

    except Exception:
        logger.exception("mongo_vector_search failed")
        return None, 0.0
