from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from pymongo import MongoClient
import openai
import os
import json
from typing import Optional, List, Dict, Any

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token  # <-- import your auth here

# -------------------------------
# MongoDB and OpenAI setup
# -------------------------------
router = APIRouter(
    prefix="/faults",
    tags=["Operations"]
)
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
faults_collection = db["Faults"]

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

class FaultRequest(BaseModel):
    """Which category and locale to produce a fault list for. Both are mandatory."""

    category: str = Field(
        ...,
        description="**Mandatory.** Device category, matched **exactly** against the stored `Category`. Unknown categories return `404`.",
        examples=["Dishwasher"],
    )
    locale: str = Field(
        ...,
        description="**Mandatory.** Locale the faults should be written in, e.g. `fr_FR`.",
        examples=["fr_FR"],
    )

    model_config = {"json_schema_extra": {"example": {"category": "Dishwasher", "locale": "fr_FR"}}}

def find_category(faults_collection, category: str) -> Optional[dict]:
    """Look up a document by category in the faults collection."""
    return faults_collection.find_one({"Category": category})

def locale_exists(doc: dict, locale: str) -> Optional[dict]:
    """Check if a specific locale exists within the Content array of the document."""
    for content in doc.get("Content", []):
        if content.get("locale") == locale:
            return content
    return None

def add_locale_faults(faults_collection, doc_id, locale: str, faults: List[Dict[str, Any]]) -> None:
    """Push a new locale with faults into the Content array for the document with doc_id."""
    new_content = {"locale": locale, "Faults": faults}
    faults_collection.update_one({"_id": doc_id}, {"$push": {"Content": new_content}})

def generate_faults_via_openai(category: str, locale: str, model: str = "gpt-4o") -> List[Dict[str, Any]]:
    """
    Uses OpenAI ChatCompletion (openai>=1.0.0) to generate a list of faults for the given category and locale.
    Handles code block Markdown-wrapped JSON as well as raw JSON.
    """
    prompt = (
        f"The user will supply a string containing a type of device. Please list 5 of the most common issues or failures that most likely occur with this product that would usually be covered by an extended warranty. Provide the specific name of parts linked to the failures if possible. "
        f"include one example of accidental damage. ensure the responses for all values are translated for locale {locale}. "
        f"Please also give an example for each one in how the issues can typically be resolved by a repairer. "
        f"Ensure that the accidental damage example is realistic for the type of device. "
        f"Keep each issue description to around 15 words, but at least 10. "
        f'JSON array format example. '
        f'{{"issues": [ {{"Issue": "...", "Description": "...", "Solution": "..." }} ]}}'
        f"\nDevice: {category}"
    )
    response = openai_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    )
    content_text = response.choices[0].message.content
    # --- Robust code block/Markdown JSON parsing
    if content_text.strip().startswith("```"):
        content_text = content_text.strip()
        # Remove ```json or ```
        if content_text.startswith("```json"):
            content_text = content_text[7:]
        elif content_text.startswith("```"):
            content_text = content_text[3:]
        # Remove trailing ```
        if content_text.endswith("```"):
            content_text = content_text[:-3]
        content_text = content_text.strip()
    try:
        output = json.loads(content_text)
        faults = output["issues"]
        assert isinstance(faults, list) and len(faults) > 0
    except Exception as e:
        raise ValueError(f"OpenAI returned invalid or unparsable content: {str(e)}\nRaw: {content_text}")
    return faults

@router.post(
    "/generate_faults",
    summary="Get (or generate) the localized fault list for a category",
    response_description="The fault list for that category and locale, plus what the call did.",
    responses=secured({
        200: json_response(
            "Faults are available. `message` says whether they were already stored or generated now.",
            {
                "message": "Locale 'fr_FR' added for category 'Dishwasher'",
                "Faults": [
                    "Ne démarre pas",
                    "N'évacue pas l'eau",
                    "Fuite d'eau",
                    "Vaisselle non lavée correctement",
                ],
            },
        ),
        404: error("No stored category matches this name exactly.", "no category found"),
        500: error("The language model call or its parsing failed. Nothing was stored — retry.", "OpenAI or parsing error: ..."),
    }),
)
def generate_faults(
    req: FaultRequest,
    _: None = Depends(verify_token)
):
    """
    Return the list of common faults for a device category, in a given language — the options
    shown when a customer reports a problem.

    **Cached, generated on first use.** If the category already has faults for that locale they
    are returned as-is. If not, they are generated with a language model, **stored against the
    category**, and returned — so the first call for a new locale is slow and every later call
    is fast. `message` tells you which happened.

    The category must already exist and is matched **exactly** — `Dishwasher` and `dishwasher`
    are not the same — and an unknown one returns `404` rather than inventing a category.

    Both fields are mandatory. A generation failure returns `500` and stores nothing, so retrying
    is safe.
    """
    # Look up the category in Mongo
    doc = find_category(faults_collection, req.category)
    if not doc:
        raise HTTPException(status_code=404, detail="no category found")

    # See if locale exists for this category
    locale_doc = locale_exists(doc, req.locale)
    if locale_doc:
        return {
            "message": "Locale already exists for this category",
            "Faults": locale_doc["Faults"]
        }

    # Generate faults for this locale
    try:
        faults = generate_faults_via_openai(req.category, req.locale)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenAI or parsing error: {e}")

    # Store the new locale faults
    add_locale_faults(faults_collection, doc["_id"], req.locale, faults)

    return {
        "message": f"Locale '{req.locale}' added for category '{req.category}'",
        "Faults": faults
    }
