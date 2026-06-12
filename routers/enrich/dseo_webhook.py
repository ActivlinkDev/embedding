import os
import logging
import sys
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dseo", tags=["Enrichment"])

mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["Activlink"]
dseo_results_collection = db["DSEO_Results"]


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@router.post("/webhook")
async def dseo_webhook(request: Request):
    """
    Receives DataforSEO postback callbacks for merchant/google/products tasks.
    The raw payload is stored in the DSEO_Results collection, keyed by the
    DataforSEO task id (passed via the ?id= query parameter).
    """
    task_id = request.query_params.get("id")

    try:
        body = await request.json()
    except Exception as e:
        # Accept and log even if JSON is malformed — always return 200 to DataforSEO
        print(f"[DSEO Webhook] Failed to parse JSON body: {e}", file=sys.stderr)
        body = {}

    print(f"[DSEO Webhook] Received postback task_id={task_id}", file=sys.stderr)

    record = {
        "task_id": task_id,
        "received_at": _utc_now_iso(),
        "payload": body,
    }

    try:
        result = dseo_results_collection.insert_one(record)
        print(f"[DSEO Webhook] Stored result _id={result.inserted_id} task_id={task_id}", file=sys.stderr)
    except Exception as e:
        # Log but don't return an error — DataforSEO requires 200 OK or it will retry
        print(f"[DSEO Webhook] DB insert failed: {e}", file=sys.stderr)

    return JSONResponse(content={"status": "ok"}, status_code=200)
