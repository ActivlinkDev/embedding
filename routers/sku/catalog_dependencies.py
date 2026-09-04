"""Mongo collections and the shared catalogue service."""

import os

from dotenv import load_dotenv
from pymongo import MongoClient

from services.catalog import CatalogService


load_dotenv()

mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    raise RuntimeError("MONGO_URI not set in environment")

mongo_client = MongoClient(mongo_uri)
database = mongo_client["Activlink"]
master_collection = database["MasterSKU"]
custom_collection = database["CustomSKU"]
locale_collection = database["Locale_Params"]
client_collection = database["ClientKey"]

catalog = CatalogService(
    master_collection,
    custom_collection,
    locale_collection,
    client_collection,
)

