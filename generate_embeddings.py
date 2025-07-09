from dotenv import load_dotenv
load_dotenv()

import os
import json
import openai
from time import sleep

openai.api_key = os.getenv("OPENAI_API_KEY")

# Load the category list
with open("category.json", "r") as f:
    categories = json.load(f)

# Safety check
if not all(isinstance(c, str) for c in categories):
    raise ValueError("All items in category list must be strings.")

# Function to embed in batches
def batch_embed_texts(texts, batch_size=100, delay=1):
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        print(f"Embedding batch {i}–{i+len(batch)-1}...")
        try:
            response = openai.embeddings.create(
                model="text-embedding-3-large",
                input=batch
            )
            embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(embeddings)
            sleep(delay)  # Prevent rate limiting
        except Exception as e:
            print(f"❌ Error at batch {i}: {e}")
            break
    return all_embeddings

# Run embedding
embeddings = batch_embed_texts(categories)

# Save embeddings to file
with open("category_embeddings.json", "w") as f:
    json.dump(embeddings, f)

print("✅ Embeddings saved successfully.")
