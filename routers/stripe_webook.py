from fastapi import APIRouter, Request, HTTPException, Header
import stripe
import os
import sys
from pymongo import MongoClient

router = APIRouter(tags=["Stripe Webhook"])

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
if not STRIPE_WEBHOOK_SECRET:
    raise RuntimeError("STRIPE_WEBHOOK_SECRET must be set as an environment variable.")

STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
if STRIPE_API_KEY:
    stripe.api_key = STRIPE_API_KEY

client = MongoClient(os.getenv("MONGO_URI"))
db = client["Activlink"]
stripe_completed_collection = db["Stripe_completed"]

@router.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature")
):
    # Log incoming request details
    payload = await request.body()
    print(f"[Stripe Webhook] Received payload: {payload}", file=sys.stderr)
    print(f"[Stripe Webhook] Received stripe-signature: {stripe_signature}", file=sys.stderr)

    # Try to parse and verify the webhook event
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=stripe_signature,
            secret=STRIPE_WEBHOOK_SECRET
        )
        print(f"[Stripe Webhook] Event parsed successfully: {event.get('type')}", file=sys.stderr)
    except ValueError as e:
        print(f"[Stripe Webhook] Invalid payload: {e}", file=sys.stderr)
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        print(f"[Stripe Webhook] Invalid Stripe signature: {e}", file=sys.stderr)
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except Exception as e:
        print(f"[Stripe Webhook] Unexpected error during event parsing: {e}", file=sys.stderr)
        raise HTTPException(status_code=400, detail=f"Unexpected error: {e}")

    event_type = event.get('type')
    data = event['data']['object']

    try:
        if event_type == "checkout.session.completed":
            record = {
                "event_type": event_type,
                "session": data,
                "received_at": event.get("created"),
                "stripe_event_id": event["id"],
                "raw_event": event
            }
            result = stripe_completed_collection.insert_one(record)
            print(f"[Stripe Webhook] Stored completed session in DB with _id: {result.inserted_id}", file=sys.stderr)
        else:
            print(f"[Stripe Webhook] Received event type: {event_type} (not stored).", file=sys.stderr)
    except Exception as db_exc:
        print(f"[Stripe Webhook] DB error: {db_exc}", file=sys.stderr)
        raise HTTPException(status_code=500, detail=f"DB error: {db_exc}")

    print(f"[Stripe Webhook] Processing complete, returning success.", file=sys.stderr)
    return {"status": "success"}
