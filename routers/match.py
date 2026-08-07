from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from typing import Optional
import os
from pymongo import MongoClient

from utils.api_docs import error, json_response, secured
from utils.common import embed_query, cosine_similarity
from utils.dependencies import verify_token

router = APIRouter(
    tags=["Catalog"]
)

class QueryRequest(BaseModel):
    """Free text to classify into a device category."""

    query: str = Field(
        ...,
        description=(
            "**Mandatory.** Any product description — a title, a marketing blurb, a few "
            "keywords. It is embedded and compared against the category vectors."
        ),
        examples=["Bosch Series 6 freestanding dishwasher, 60cm, stainless steel"],
    )
    # optional preferred locale, e.g. 'en_GB', 'fr_FR'
    locale: Optional[str] = Field(
        None,
        description=(
            "Optional. Preferred locale for `locale_title`. When the matched category has no "
            "title in this locale the response falls back to `en_GB`, then to any available "
            "title. Does not affect which category is matched."
        ),
        examples=["fr_FR"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "query": "Bosch Series 6 freestanding dishwasher, 60cm, stainless steel",
                "locale": "en_GB",
            }
        }
    }

class MatchResponse(BaseModel):
    """The single best-matching category."""

    category: str = Field(
        ...,
        description="The matched category name. **Empty string when nothing matched.**",
        examples=["Dishwasher"],
    )
    similarity: float = Field(
        ...,
        description=(
            "Vector-search score for the match, or a cosine similarity computed locally when the "
            "search backend returns no score. `0.0` when nothing matched. There is no minimum "
            "threshold — always check the value before trusting the category."
        ),
        examples=[0.8734],
    )
    # localized title for the matched category (if found)
    locale_title: Optional[str] = Field(
        None,
        description="Localized display title for the category, if the record carries one.",
        examples=["Lave-vaisselle"],
    )


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
            # Bounded timeouts so a stuck/unhealthy Atlas Search backend can never
            # block a request (and therefore a uvicorn worker thread) indefinitely.
            _mongo_client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=8000,
            )
        except Exception:
            _mongo_client = None
    return _mongo_client

@router.post(
    "/match",
    response_model=MatchResponse,
    summary="Classify free text into a device category",
    response_description="The best-matching category, its score, and a localized title.",
    responses=secured({
        200: json_response(
            "A match, or an empty result. Both are `200` — check `category` and `similarity`.",
            {"category": "Dishwasher", "similarity": 0.8734, "locale_title": "Dishwasher"},
        ),
        500: error("The embedding request to OpenAI failed or timed out.", "OpenAI API error"),
    }),
)
def match_category(
    request: QueryRequest,
    _: None = Depends(verify_token)
):
    """
    Match a free-text product description to a device category using vector search.

    The text is embedded with OpenAI, then compared against the category embeddings held in the
    `Category` collection via Atlas `$vectorSearch`. Only the single best candidate is returned.

    **No-match is not an error.** If the search backend is unavailable, times out, or returns
    nothing, the response is still `200` with `{"category": "", "similarity": 0.0}`. Always check
    `category` before using the result — and treat `similarity` as advisory, since no minimum
    threshold is enforced here.

    That fallback covers the **search** stage only. Embedding happens first and is not guarded:
    if the OpenAI request fails or times out, the call returns `500` rather than an empty match.

    `query` is mandatory. `locale` only affects `locale_title`, never the match itself.
    """
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

            # maxTimeMS bounds the server-side $vectorSearch so a hung Atlas Search
            # node raises (caught below) instead of hanging the request forever.
            results = list(coll.aggregate([stage], maxTimeMS=5000))
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
