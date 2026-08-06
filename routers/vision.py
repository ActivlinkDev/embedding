from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, HttpUrl
from typing import Dict, Any
import openai
import os
import json

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token

# --- Import your category matching tools ---
# `find_best_match` and embedding helpers live in `utils.common`.
# Import them from there to avoid circular/misplaced imports.
from utils.common import find_best_match, embed_query, category_embeddings, device_categories
# Update the import above if your project structure differs

router = APIRouter(
    prefix="/vision",
    tags=["Enrichment"]
)

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_device_info_schema() -> Dict[str, Any]:
    """
    Returns the strict JSON schema used for extracting device info from the image.
    """
    return {
        "type": "object",
        "properties": {
            "make": {"type": "string", "description": "The manufacturer of the device."},
            "model": {"type": "string", "description": "The specific model of the device."},
            "serial": {"type": "string", "description": "The specific serial number of the device."},
            "device_category": {"type": "string", "description": "The category of the device (e.g., mobile, tablet, laptop)."},
            "country": {"type": "string", "description": "The country where the device is manufactured or intended for use."},
        },
        "required": ["make", "model", "serial", "device_category", "country"],
        "additionalProperties": False,
        "strict": True,
    }

def compose_vision_messages(image_url: str) -> list:
    """
    Builds the OpenAI messages array for GPT-4o vision input, using correct content types and image_url as an object.
    The prompt must mention "json" to use response_format=json_object.
    """
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "determine the correct values from this image and respond in a JSON object"},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
        }
    ]

def get_tools_for_vision() -> list:
    """
    Returns the tools list with function-calling and correct type.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "device_info",
                "description": "Extracted device information from the image.",
                "parameters": get_device_info_schema()
            }
        }
    ]

def extract_device_info_from_image(image_url: str, model: str = "gpt-4o") -> Dict[str, Any]:
    """
    Calls OpenAI GPT-4o with image URL, using function-calling to extract device info.
    Returns parsed result matching the strict device_info schema.
    Raises ValueError on parse/call error.
    """
    messages = compose_vision_messages(image_url)
    tools = get_tools_for_vision()
    try:
        response = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "device_info"}},
            max_tokens=512,
            temperature=0,
            response_format={"type": "json_object"}
        )
        content = response.choices[0].message
        if hasattr(content, "tool_calls") and content.tool_calls:
            arguments = content.tool_calls[0].function.arguments
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            return arguments
        if content.content:
            if isinstance(content.content, str):
                return json.loads(content.content)
            return content.content
        raise ValueError("OpenAI did not return device info.")
    except Exception as e:
        raise ValueError(f"OpenAI Vision API error: {e}")

class DeviceImageRequest(BaseModel):
    """The image to read device details from."""

    image_url: HttpUrl = Field(
        ...,
        description=(
            "**Mandatory.** Publicly reachable URL of the photo — typically a rating plate or "
            "serial label. Must be fetchable by OpenAI; a signed or private URL will fail."
        ),
        examples=["https://cdn.example.com/uploads/rating-plate-8841203.jpg"],
    )

    model_config = {
        "json_schema_extra": {
            "example": {"image_url": "https://cdn.example.com/uploads/rating-plate-8841203.jpg"}
        }
    }

@router.post(
    "/device_info_from_image",
    summary="Read device details from a photo of its label",
    response_description="The details read from the image, plus the category it was matched to.",
    responses=secured({
        200: json_response(
            "The image was read. **Values are model output** — verify before storing.",
            {
                "device_info": {
                    "make": "Bosch",
                    "model": "SMS6ZCI00G",
                    "serial": "SN-8841203",
                    "device_category": "dishwasher",
                    "country": "Germany",
                    "matched_category": "Dishwasher",
                    "match_similarity": 0.91,
                }
            },
        ),
        500: error(
            "The image could not be fetched or read — unreachable URL, unreadable photo, or a "
            "vision API failure.",
            "OpenAI Vision API error: ...",
        ),
    }),
)
def device_info_from_image(
    req: DeviceImageRequest,
    _: None = Depends(verify_token)
):
    """
    Read a device's details from a photo — typically the rating plate or serial label a customer
    photographs during registration.

    The image is analysed by a vision model, which returns `make`, `model`, `serial`,
    `device_category` and `country`. The free-text category is then matched against the known
    category list, adding `matched_category` and a `match_similarity` score.

    **Every value is model output and can be wrong.** Blank strings come back for fields the
    model could not read, and `match_similarity` is worth checking before trusting
    `matched_category`. Treat the result as a form pre-fill for the customer to confirm, not as
    verified data.

    `image_url` is mandatory and must be **publicly reachable** — the image is fetched by the
    vision API, not proxied through this service. Nothing is stored: the response is the only
    output.
    """
    try:
        device_info = extract_device_info_from_image(str(req.image_url))
        gpt_category = device_info.get("device_category", "")
        if gpt_category:
            embedding = embed_query(gpt_category)
            matched_category, similarity = find_best_match(embedding, category_embeddings, device_categories)
            device_info["matched_category"] = matched_category
            device_info["match_similarity"] = similarity

        return {
            "device_info": device_info
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
