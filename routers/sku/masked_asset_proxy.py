"""Serve MasterSKU documents through masked, non-expiring Activlink URLs.

`create_master_sku` writes a `url_map` entry for every Icecat document it stores and hands
out `/sku/r/{key}` in the master's assets, so those keys are persisted in MasterSKU records
and this route has to keep resolving them for as long as the records exist.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
import httpx
from starlette.background import BackgroundTask

from .catalog_dependencies import database


router = APIRouter(prefix="/sku", tags=["Catalog"])
url_map_collection = database["url_map"]


async def _close_upstream(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


@router.get("/r/{key}")
async def proxy_masked(key: str):
    doc = url_map_collection.find_one({"_id": key})
    if not doc or not doc.get("url"):
        raise HTTPException(status_code=404, detail="Not found")
    expires_at = doc.get("expires_at")
    if isinstance(expires_at, datetime):
        comparable = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if comparable < datetime.now(timezone.utc):
            raise HTTPException(status_code=404, detail="Not found")
    try:
        client = httpx.AsyncClient(timeout=30.0)
        try:
            upstream_request = client.build_request("GET", doc["url"])
            response = await client.send(upstream_request, stream=True, follow_redirects=True)
        except Exception:
            await client.aclose()
            raise
        headers = {
            name: value for name, value in response.headers.items()
            if name.lower() not in {"connection", "transfer-encoding"}
        }
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=headers,
            media_type=response.headers.get("content-type"),
            background=BackgroundTask(_close_upstream, response, client),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {exc}")
