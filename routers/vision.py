from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, HttpUrl
from typing import Dict, Any
import openai
import os
import json

from utils.dependencies import verify_token

# --- Import your category matching tools ---
from routers.match import find_best_match, embed_query, category_embeddings, device_categories
# Update the import above if your project structure differs

router = APIRouter(
    prefix="/vision",
    tags=["Vision"]
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
    image_url: HttpUrl

@router.post("/device_info_from_image")
def device_info_from_image(
    req: DeviceImageRequest,
    _: None = Depends(verify_token)
):
    """
    Receives an image URL, uses OpenAI GPT-4o vision to extract device info.
    Matches the category and returns both inside device_info.
    Auth required.
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
