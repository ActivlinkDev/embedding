from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
import os
from pymongo import MongoClient

from utils.common import embed_query, cosine_similarity
from utils.dependencies import verify_token

router = APIRouter(
    tags=["Match"]
)

class QueryRequest(BaseModel):
    query: str
    # optional preferred locale, e.g. 'en_GB', 'fr_FR'
    locale: Optional[str] = None

class MatchResponse(BaseModel):
    category: str
    similarity: float
    # localized title for the matched category (if found)
    locale_title: Optional[str] = None


# Mongo configuration (used only for lookup; missing MONGO_URI will be tolerated)
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB_NAME", "Activlink")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "Category")

_mongo_client = None
def _get_mongo_client():
    global _mongo_client
    if _mongo_client is None:
        if not MONGO_URI:
            return None
        try:
            _mongo_client = MongoClient(MONGO_URI)
        except Exception:
            _mongo_client = None
    return _mongo_client

@router.post("/match", response_model=MatchResponse)
def match_category(
    request: QueryRequest,
    _: None = Depends(verify_token)
):
    # Embed the incoming query
    query_embedding = embed_query(request.query)

    # Try using MongoDB's vector search first (preferred). If unavailable or
    # it fails, fall back to the in-memory category_embeddings lookup.
    locale_title = None
    matched_category = None
    matched_score = None

    try:
        client = _get_mongo_client()
        if client:
            db = client[MONGO_DB]
            coll = db[MONGO_COLLECTION]

            # Ensure we have a plain Python list of floats
            try:
                qvec = list(query_embedding)
            except Exception:
                qvec = [float(x) for x in query_embedding]

            index = os.getenv("VECTOR_INDEX", "vector_index")
            num_candidates = int(os.getenv("VECTOR_NUM_CANDIDATES", "100"))
            # Ask the server for the single best match
            stage = {
                "$vectorSearch": {
                    "index": index,
                    "path": "embedding",
                    "queryVector": qvec,
                    "numCandidates": num_candidates,
                    "limit": 1,
                }
            }

            results = list(coll.aggregate([stage]))
            if results:
                doc = results[0]
                # category field may have different names in documents
                cat = doc.get("category") or doc.get("Category") or doc.get("category_name")

                # Extract score if provided by server, else compute cosine similarity
                score = None
                for k in ("score", "searchScore", "vectorSearchScore", "scoreValue", "_score"):
                    if k in doc:
                        try:
                            score = float(doc[k])
                        except Exception:
                            score = None
                        break

                if score is None and isinstance(doc.get("embedding"), (list, tuple)):
                    try:
                        score = float(cosine_similarity(query_embedding, doc["embedding"]))
                    except Exception:
                        score = None

                matched_category = cat
                matched_score = float(score) if score is not None else 0.0

                # If the document contains localized titles, pick the preferred one
                if isinstance(doc.get("locale_title"), list):
                    titles = {lt.get("locale"): lt.get("title") for lt in doc.get("locale_title", []) if lt.get("locale") and lt.get("title")}
                    req = request.locale
                    if req and req in titles:
                        locale_title = titles[req]
                    elif "en_GB" in titles:
                        locale_title = titles["en_GB"]
                    elif titles:
                        locale_title = next(iter(titles.values()))
    except Exception:
        # Do not raise here; we'll return an empty/zero-match result below.
        matched_category = None
        matched_score = None
    # If no match found or Mongo unavailable, return an empty category with 0.0 similarity
    if not matched_category:
        return MatchResponse(category="", similarity=0.0, locale_title=locale_title)

    return MatchResponse(category=matched_category, similarity=float(matched_score or 0.0), locale_title=locale_title)
