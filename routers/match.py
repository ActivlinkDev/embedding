from fastapi import APIRouter, Depends
from pydantic import BaseModel
from utils.common import embed_query, find_best_match, category_embeddings, device_categories
from utils.dependencies import verify_token

router = APIRouter(
    tags=["Match"]
)

class QueryRequest(BaseModel):
    query: str

class MatchResponse(BaseModel):
    category: str
    similarity: float

@router.post("/match", response_model=MatchResponse)
def match_category(
    request: QueryRequest,
    _: None = Depends(verify_token)
):
    query_embedding = embed_query(request.query)
    category, similarity = find_best_match(query_embedding, category_embeddings, device_categories)
    return MatchResponse(category=category, similarity=similarity)
