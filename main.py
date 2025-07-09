import os
import numpy as np
import requests
from fastapi import FastAPI
from pydantic import BaseModel
import openai
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

openai.api_key = os.getenv("OPENAI_API_KEY")
if not openai.api_key:
    logger.error("OPENAI_API_KEY not set!")
    raise ValueError("OPENAI_API_KEY not set")

app = FastAPI()

device_categories_url = "https://tmpfiles.org/dl/5288739/category.json"

def fetch_device_categories(url):
    logger.info("Fetching device categories...")
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

try:
    device_categories = fetch_device_categories(device_categories_url)
    logger.info(f"Fetched {len(device_categories)} categories.")
except Exception as e:
    logger.error(f"Failed to fetch device categories: {e}")
    raise

def embed_texts(texts):
    logger.info("Generating category embeddings...")
    response = openai.embeddings.create(
        model="text-embedding-3-large",
        input=texts
    )
    embeddings = [item.embedding for item in response.data]
    return embeddings

try:
    category_embeddings = embed_texts(device_categories)
    logger.info("Generated category embeddings.")
except Exception as e:
    logger.error(f"Embedding failed: {e}")
    raise
