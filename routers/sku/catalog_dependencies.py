"""Mongo collections and the shared catalogue service."""

import os

from dotenv import load_dotenv

from services.catalog import CatalogService
from utils.mongo import require_client


load_dotenv()

mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    raise RuntimeError("MONGO_URI not set in environment")

mongo_client = require_client()
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

