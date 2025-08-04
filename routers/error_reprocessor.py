# error_reprocessor.py

import asyncio
from datetime import datetime
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

from routers.sku.create_custom_sku import create_custom_sku, CustomSKURequest, LocaleDetails, CustomLink

load_dotenv()

# === MongoDB Setup ===
MONGO_URI = os.getenv("MONGO_URI")
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["Activlink"]
error_log_collection = db["Error_Log_Lookup_Custom_SKU"]

# === Background Task ===
async def error_reprocessor_loop():
    print("⏳ Starting background error reprocessing task...")
    while True:
        try:
            async for doc in error_log_collection.find({"status": "error"}):
                doc_id = doc["_id"]
                payload = doc.get("payload", {})
                retry_count = doc.get("retry_count", 0)

                try:
                    # Use 'locale' as the locale string and 'Locale_Details' for details
                    locale_code = payload.get("locale", "")
                    locale_detail_data = payload.get("Locale_Details", {})

                    locale_details = LocaleDetails(
                        Title=locale_detail_data.get("Title", ""),
                        Price=locale_detail_data.get("Price", 0),
                        GTL=locale_detail_data.get("GTL", 0),
                        GTP=locale_detail_data.get("GTP", 0),
                        Promo_Code=locale_detail_data.get("Promo_Code", ""),
                        Custom_Links=[CustomLink(**cl) for cl in locale_detail_data.get("Custom_Links", [])]
                        if locale_detail_data.get("Custom_Links") else None
                    )

                    transformed_payload = {
                        "ClientKey": payload.get("clientKey"),
                        "Locale": locale_code,
                        "SKU": payload.get("SKU"),
                        "Source": payload.get("source", "API_Reprocessor"),
                        "GTIN": payload.get("GTIN", ""),
                        "Make": payload.get("Make", ""),
                        "Model": payload.get("Model", ""),
                        "Category": payload.get("Category", ""),
                        "Locale_Details": locale_details
                    }

                    request = CustomSKURequest(**transformed_payload)
                    result = create_custom_sku(request, None)

                    await error_log_collection.update_one(
                        {"_id": doc_id},
                        {"$set": {
                            "status": "reprocessed",
                            "reprocessed_at": datetime.utcnow(),
                            "result": result,
                            "retry_count": retry_count + 1
                        }}
                    )
                    print(f"✅ Reprocessed: {doc_id}")

                except Exception as e:
                    await error_log_collection.update_one(
                        {"_id": doc_id},
                        {"$set": {
                            "status": "reprocess_failed",
                            "error": str(e),
                            "attempted_at": datetime.utcnow(),
                            "retry_count": retry_count + 1
                        }}
                    )
                    print(f"❌ Failed to reprocess {doc_id}: {e}")

        except Exception as loop_error:
            print(f"🔥 Error in reprocessor loop: {loop_error}")
        await asyncio.sleep(20)  # Run every 5 minutes


# === Run Directly ===
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(error_reprocessor_loop())
