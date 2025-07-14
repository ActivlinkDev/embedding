from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional
from pymongo import MongoClient
from datetime import datetime
import os
from dotenv import load_dotenv

from utils.dependencies import verify_token

load_dotenv()

router = APIRouter(
    prefix="/sku",
    tags=["Create Custom SKU"]
)

# MongoDB connection
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
collection = db["CustomSKU"]

# --- Pydantic Models ---

class GuaranteeModel(BaseModel):
    Labour: int
    Parts: int
    Promotion: Optional[str]

class LinkModel(BaseModel):
    QR: Optional[str]
    Service_URL: Optional[str]

class LocaleDataModel(BaseModel):
    locale: str
    Title: str
    Category: str
    Generate_Offers: str
    MSRP: float
    Currency: str
    created_at: Optional[str] = Field(default_factory=lambda: datetime.utcnow().isoformat())
    Guarantees: GuaranteeModel
    Custom_Links: Optional[List[LinkModel]]

class IdentifierModel(BaseModel):
    GTIN: List[str]  # ✅ GTIN is now a list
    Make: str
    SKU: str  # ✅ Required
    Model: str

class SKUCreateModel(BaseModel):
    Client: str  # ✅ Required
    Sources: List[str]
    Identifiers: IdentifierModel
    Locale_Specific_Data: List[LocaleDataModel]

# --- Endpoint ---

@router.post("/create_customsku", dependencies=[Depends(verify_token)])
def create_sku(payload: SKUCreateModel):
    try:
        # Check for existing document with same Client + SKU + any GTIN match
        existing = collection.find_one({
            "Client": payload.Client,
            "Identifiers.SKU": payload.Identifiers.SKU,
            "Identifiers.GTIN": {"$in": payload.Identifiers.GTIN}
        })

        if existing:
            raise HTTPException(
                status_code=400,
                detail="A document with this SKU and one of the provided GTINs already exists."
            )

        # Convert to dict and insert
        document = payload.dict()
        result = collection.insert_one(document)

        return {
            "message": "SKU document inserted successfully.",
            "inserted_id": str(result.inserted_id)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
