"""Reset the catalogue to the empty v2 schema.

This intentionally destroys MasterSKU and CustomSKU data.  It also clears the
derived widget cache and legacy lookup-reprocessing queue.  Device,
registration, quote and QR history are preserved.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient


MASTER_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "schemaVersion", "matchKey", "identifiers", "locales", "createdAt", "updatedAt"
        ],
        "properties": {
            "schemaVersion": {"enum": [2]},
            "matchKey": {"bsonType": "string", "minLength": 1},
            "identifiers": {
                "bsonType": "object",
                "required": ["make", "makeNormalized", "model", "modelNormalized", "gtins"],
                "properties": {
                    "make": {"bsonType": "string"},
                    "makeNormalized": {"bsonType": "string"},
                    "model": {"bsonType": "string"},
                    "modelNormalized": {"bsonType": "string"},
                    "gtins": {"bsonType": "array", "items": {"bsonType": "string"}},
                },
            },
            "category": {"bsonType": ["string", "null"]},
            "imageUrl": {"bsonType": ["string", "null"]},
            "locales": {"bsonType": "object"},
            "provenance": {"bsonType": "object"},
            "createdAt": {"bsonType": "date"},
            "updatedAt": {"bsonType": "date"},
        },
    }
}

CUSTOM_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "schemaVersion", "clientId", "masterSkuId", "sku", "skuNormalized",
            "enabledLocales", "sources", "overrides", "createdAt", "updatedAt"
        ],
        "properties": {
            "schemaVersion": {"enum": [2]},
            "clientId": {"bsonType": ["string", "objectId"]},
            "masterSkuId": {"bsonType": "objectId"},
            "sku": {"bsonType": "string", "minLength": 1},
            "skuNormalized": {"bsonType": "string", "minLength": 1},
            "enabledLocales": {
                "bsonType": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"bsonType": "string"},
            },
            "sources": {
                "bsonType": "array",
                "items": {"bsonType": "string"},
            },
            "overrides": {"bsonType": "object"},
            "createdAt": {"bsonType": "date"},
            "updatedAt": {"bsonType": "date"},
        },
    }
}


def recreate(db, name: str, validator: dict):
    if name in db.list_collection_names():
        db[name].drop()
    return db.create_collection(
        name,
        validator=validator,
        validationLevel="strict",
        validationAction="error",
    )


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-delete", action="store_true")
    parser.add_argument("--database", default=os.getenv("MONGO_DB", "Activlink"))
    args = parser.parse_args()
    if not args.confirm_delete:
        print("Refusing to reset without --confirm-delete", file=sys.stderr)
        return 2

    uri = os.getenv("MONGO_URI")
    if not uri:
        print("MONGO_URI is not configured", file=sys.stderr)
        return 2

    with MongoClient(uri, serverSelectionTimeoutMS=15000) as client:
        db = client[args.database]
        before = {
            "MasterSKU": db["MasterSKU"].count_documents({}),
            "CustomSKU": db["CustomSKU"].count_documents({}),
            "WidgetQuoteCache": db["WidgetQuoteCache"].count_documents({}),
            "Error_Log_Lookup_Custom_SKU": db["Error_Log_Lookup_Custom_SKU"].count_documents({}),
        }

        masters = recreate(db, "MasterSKU", MASTER_VALIDATOR)
        customs = recreate(db, "CustomSKU", CUSTOM_VALIDATOR)
        db["WidgetQuoteCache"].delete_many({})
        db["Error_Log_Lookup_Custom_SKU"].delete_many({})

        masters.create_index("matchKey", unique=True, name="uniq_master_match_key")
        masters.create_index("identifiers.gtins", name="master_gtins")
        masters.create_index(
            [("identifiers.makeNormalized", 1), ("identifiers.modelNormalized", 1)],
            name="master_make_model",
        )
        customs.create_index(
            [("clientId", 1), ("skuNormalized", 1)],
            unique=True,
            name="uniq_client_sku",
        )
        customs.create_index(
            [("clientId", 1), ("masterSkuId", 1)],
            name="client_master",
        )
        customs.create_index(
            [("clientId", 1), ("enabledLocales", 1)],
            name="client_locales",
        )
        db["url_map"].create_index(
            "expires_at",
            expireAfterSeconds=0,
        )

        after = {
            "MasterSKU": masters.count_documents({}),
            "CustomSKU": customs.count_documents({}),
            "WidgetQuoteCache": db["WidgetQuoteCache"].count_documents({}),
            "Error_Log_Lookup_Custom_SKU": db["Error_Log_Lookup_Custom_SKU"].count_documents({}),
        }
        print({"deleted": before, "remaining": after})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
