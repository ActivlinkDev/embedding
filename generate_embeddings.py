from dotenv import load_dotenv
load_dotenv()

import os
import openai
import numpy as np
from pymongo import MongoClient
from time import sleep
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

openai.api_key = os.getenv("OPENAI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = "Activlink"
COLLECTION_NAME = "Category"
OUTPUT_FILE = "generate_embeddings.npz"

if not openai.api_key:
    logger.error("OPENAI_API_KEY not set!")
    raise ValueError("OPENAI_API_KEY not set!")
if not MONGO_URI:
    logger.error("MONGO_URI not set!")
    raise ValueError("MONGO_URI not set!")

# Connect and fetch categories
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
category_collection = db[COLLECTION_NAME]
categories = category_collection.distinct("category")

if not categories:
    logger.error("No categories found in the collection!")
    raise ValueError("No categories found in the collection!")

logger.info(f"Categories found: {len(categories)}")
logger.info(categories)

if not all(isinstance(c, str) for c in categories):
    raise ValueError("All items in category list must be strings.")

def batch_embed_texts(texts, batch_size=100, delay=1):
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        logger.info(f"Embedding batch {i}–{i+len(batch)-1}...")
        try:
            response = openai.embeddings.create(
                model="text-embedding-3-large",
                input=batch
            )
            embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(embeddings)
            sleep(delay)  # Prevent rate limiting
        except Exception as e:
            logger.error(f"❌ Error at batch {i}: {e}")
            break
    return all_embeddings

embeddings = batch_embed_texts(categories)
embeddings_array = np.array(embeddings, dtype=np.float32)

# Save embeddings and categories together
np.savez_compressed(OUTPUT_FILE, embeddings=embeddings_array, categories=np.array(categories))
logger.info(f"✅ Embeddings and categories saved successfully to {OUTPUT_FILE}.")
