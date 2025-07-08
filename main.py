import os
import numpy as np
import requests
from fastapi import FastAPI, Query
from pydantic import BaseModel
import openai

openai.api_key = os.getenv("OPENAI_API_KEY")

app = FastAPI()

# Fetch device categories once at startup
device_categories_url = "https://tmpfiles.org/dl/5288739/category.json"

def fetch_device_categories(url):
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

device_categories = fetch_device_categories(device_categories_url)

def embed_texts(texts):
    response = openai.embeddings.create(
        model="text-embedding-3-large",
        input=texts
    )
    embeddings = [item.embedding for item in response.data]
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
    best_idx = np.argmax(similarities)
    return categories[best_idx], float(similarities[best_idx])

class QueryRequest(BaseModel):
    query: str

class MatchResponse(BaseModel):
    category: str
    similarity: float

@app.post("/match", response_model=MatchResponse)
def match_category(request: QueryRequest):
    query_embedding = embed_query(request.query)
    category, similarity = find_best_match(query_embedding, category_embeddings, device_categories)
    return MatchResponse(category=category, similarity=similarity)
