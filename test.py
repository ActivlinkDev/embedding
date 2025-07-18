from dotenv import load_dotenv
load_dotenv()
import os
from pymongo import MongoClient

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["Category"]

docs = list(collection.find({}, {"_id": 0, "category": 1}))
print("All docs:\n", docs)
cats = collection.distinct("category")
print("Distinct categories:\n", cats)
