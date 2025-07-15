from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from typing import Dict
import os
from dotenv import load_dotenv
from openai import OpenAI

from utils.common import embed_query, find_best_match, category_embeddings, device_categories
from utils.dependencies import verify_token

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

router = APIRouter(
    prefix="/ai",
    tags=["AI Extract + Match"]
)

SYSTEM_PROMPT = """You are a product info extractor. Given a product title, extract:
- Make: brand or manufacturer
- Model: specific model code
- Category: type of appliance (e.g. Washing Machine, Fridge-Freezer, Dishwasher, Oven)

Return ONLY compact JSON like:
{ "Make": "Beko", "Model": "BM1WT3821W", "Category": "Washing Machine" }
"""

class ExtractMatchResponse(BaseModel):
    Make: str
    Model: str
    Category: str
    Matched_Category: str
    Similarity: float

@router.get("/extract-and-match", response_model=ExtractMatchResponse)
def extract_and_match(
    query: str = Query(..., description="Product title or listing"),
    _: None = Depends(verify_token)
) -> Dict:
    try:
        # Step 1: Use GPT to extract Make, Model, and Category
        chat_response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query}
            ],
            temperature=0.2,
            max_tokens=150,
        )

        raw_content = chat_response.choices[0].message.content.strip()
        extracted = eval(raw_content)

        if not all(k in extracted for k in ["Make", "Model", "Category"]):
            raise HTTPException(status_code=500, detail="GPT output missing required fields")

        # Step 2: Match Category using embedding
        embedding = embed_query(extracted["Category"])
        matched_category, similarity = find_best_match(embedding, category_embeddings, device_categories)

        return ExtractMatchResponse(
            Make=extracted["Make"],
            Model=extracted["Model"],
            Category=extracted["Category"],
            Matched_Category=matched_category,
            Similarity=similarity
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI extract/match error: {str(e)}")
