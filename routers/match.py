from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
import os
from pymongo import MongoClient

from utils.common import embed_query, find_best_match, category_embeddings, device_categories
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
    query_embedding = embed_query(request.query)
    category, similarity = find_best_match(query_embedding, category_embeddings, device_categories)
    locale_title = None
    try:
        client = _get_mongo_client()
        if client:
            db = client[MONGO_DB]
            coll = db[MONGO_COLLECTION]
            doc = coll.find_one({"category": category})
            if doc and isinstance(doc.get("locale_title"), list):
                # Build mapping locale->title
                titles = {lt.get("locale"): lt.get("title") for lt in doc.get("locale_title", []) if lt.get("locale") and lt.get("title")}
                # prefer requested locale, then en_GB, then any
                req = request.locale
                if req and req in titles:
                    locale_title = titles[req]
                elif "en_GB" in titles:
                    locale_title = titles["en_GB"]
                elif titles:
                    locale_title = next(iter(titles.values()))
    except Exception:
        # Do not raise — matching should still work even if lookup fails
        locale_title = None

    return MatchResponse(category=category, similarity=similarity, locale_title=locale_title)
