"""Serving fault photos back.

A POST rather than a GET, matching `POST /my-registrations/receipt`: the ClientKey has to
sit in the JSON body for `verify_token`'s tenant scoping to find it reliably, and a photo
of someone's home is not something to put in a URL that lands in access logs and browser
history.

Headers mirror the receipt endpoint exactly — `private, no-store` so an intermediary never
caches it, `nosniff` so the browser cannot be talked into treating it as something else,
and an inline `Content-Disposition` so it renders rather than downloads.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from utils.api_docs import error, secured
from utils.dependencies import verify_token
from utils.media import IMAGE_TYPES

from .service import media_collection, object_id, owned_request, resolve_client_id

router = APIRouter(prefix="/service-requests", tags=["Service"])


class FetchMediaRequest(BaseModel):
    clientkey: str = Field(min_length=1)
    serviceRequestId: str = Field(min_length=1)
    mediaId: str = Field(min_length=1)


@router.post(
    "/media/fetch",
    summary="Fetch a fault photo",
    response_description="The image bytes, inline and uncacheable.",
    responses=secured({
        200: {"description": "The photo.", "content": {"image/jpeg": {}}},
        404: error(
            "No such photo on a service request belonging to this client. Also returned when "
            "the stored content type is not a servable image, so an unvetted type can never "
            "reach a response header.",
            "Attachment unavailable",
        ),
    }),
)
def fetch_media(body: FetchMediaRequest, _: None = Depends(verify_token)):
    """
    Return one fault photo.

    Filtered on the photo id, its service request **and** the tenant together, so a photo
    id guessed from another tenant's request is a `404` — the same answer as one that does
    not exist.
    """
    client_id = resolve_client_id(body.clientkey)
    request_doc = owned_request(body.serviceRequestId, client_id)

    doc = media_collection.find_one({
        "_id": object_id(body.mediaId, "mediaId"),
        "serviceRequestId": request_doc["_id"],
        "client": client_id,
    })
    content_type = (doc or {}).get("contentType")
    if not doc or not doc.get("data") or content_type not in IMAGE_TYPES:
        raise HTTPException(404, "Attachment unavailable")

    return Response(bytes(doc["data"]), media_type=content_type, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline; filename*=UTF-8''" + quote(doc.get("name") or "photo", safe=""),
    })
