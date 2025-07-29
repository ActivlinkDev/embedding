from fastapi import APIRouter, Request, HTTPException, Header
import stripe
import os
from pymongo import MongoClient

router = APIRouter(tags=["Stripe Webhook"])

# --- Stripe setup ---
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
if not STRIPE_WEBHOOK_SECRET:
    raise RuntimeError("STRIPE_WEBHOOK_SECRET must be set as an environment variable.")

STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
if STRIPE_API_KEY:
    stripe.api_key = STRIPE_API_KEY

# --- MongoDB setup ---
client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
stripe_completed_collection = db["Stripe_completed"]

@router.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature")
):
    payload = await request.body()
    sig_header = stripe_signature
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")

    # --- Save to DB on session completed ---
    event_type = event['type']
    data = event['data']['object']

    if event_type == "checkout.session.completed":
        # Store full session data and event metadata
        record = {
            "event_type": event_type,
            "session": data,
            "received_at": stripe.util.convert_to_datetime(event['created']),
            "stripe_event_id": event['id'],
            "raw_event": event  # Optional: saves entire webhook event payload
        }
        try:
            stripe_completed_collection.insert_one(record)
        except Exception as db_exc:
            # Log or alert as needed
            raise HTTPException(status_code=500, detail=f"DB error: {db_exc}")

    # (Optional) handle other event types...

    return {"status": "success"}
