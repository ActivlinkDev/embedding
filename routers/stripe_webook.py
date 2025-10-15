from fastapi import APIRouter, Request, HTTPException, Header
import stripe
import os
import sys
from pymongo import MongoClient
from bson import ObjectId

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
customer_collection = db["Customer"]

# Import helper from customer router
try:
    # when running as package
    from routers.customer.create_customer import get_or_create_customer
except Exception:
    try:
        # fallback to relative file import
        from customer.create_customer import get_or_create_customer
    except Exception:
        get_or_create_customer = None

try:
    from routers.customer.pair_customer import pair_customer
except Exception:
    try:
        from customer.pair_customer import pair_customer
    except Exception:
        pair_customer = None

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
            # If we have customer details in the session, create or find the customer
            cust_details = data.get("customer_details") or {}
            if cust_details and get_or_create_customer:
                name = cust_details.get("name")
                email = cust_details.get("email")
                phone = cust_details.get("phone") or ""

                # Create or get existing customer
                try:
                    customer_id, existing = get_or_create_customer(
                        customer_collection, name or "", phone, email or ""
                    )
                    print(f"[Stripe Webhook] Customer id: {customer_id} | existing: {existing}", file=sys.stderr)

                    # Build address object from session and store on customer document
                    address = cust_details.get("address") or {}
                    if address:
                        # Normalize address fields
                        addr_obj = {
                            "line1": address.get("line1"),
                            "line2": address.get("line2"),
                            "city": address.get("city"),
                            "state": address.get("state"),
                            "postal_code": address.get("postal_code"),
                            "country": address.get("country")
                        }
                        try:
                            # Update by ObjectId (customer_id returned from helper)
                            customer_collection.update_one(
                                {"_id": ObjectId(customer_id)},
                                {"$set": {"address": addr_obj}}
                            )
                        except Exception as e:
                            print(f"[Stripe Webhook] Failed to update customer address: {e}", file=sys.stderr)
                    # Append the Stripe record to the customer's Transaction_log array
                    try:
                        customer_collection.update_one(
                            {"_id": ObjectId(customer_id)},
                            {"$push": {"transaction_log": record}}
                        )
                    except Exception as e:
                        print(f"[Stripe Webhook] Failed to append Transaction_log on customer: {e}", file=sys.stderr)
                    # Also add a contract entry into customer's contracts array if basket_id present
                    try:
                        basket_id_meta = data.get("metadata", {}).get("basket_id")
                        if basket_id_meta:
                            contract_obj = {
                                "basket_id": basket_id_meta,
                                "type": "bundle",
                                "status": "active",
                            }
                            try:
                                customer_collection.update_one(
                                    {"_id": ObjectId(customer_id)},
                                    {"$push": {"contracts": contract_obj}}
                                )
                                print(f"[Stripe Webhook] Added contract to customer: {contract_obj}", file=sys.stderr)
                            except Exception as c_err:
                                print(f"[Stripe Webhook] Failed to append contract on customer: {c_err}", file=sys.stderr)
                    except Exception as e:
                        print(f"[Stripe Webhook] Error while preparing contract object: {e}", file=sys.stderr)
                    # After customer exists/updated, attempt to pair customer to basket if metadata present
                    try:
                        basket_id = data.get("metadata", {}).get("basket_id")
                        if basket_id and pair_customer:
                            try:
                                pair_result = pair_customer(customer_id=customer_id, basket_id=basket_id)
                                print(f"[Stripe Webhook] pair_customer result: {pair_result}", file=sys.stderr)
                            except Exception as pair_exc:
                                print(f"[Stripe Webhook] pair_customer call failed: {pair_exc}", file=sys.stderr)
                    except Exception as e:
                        print(f"[Stripe Webhook] Error while attempting to pair customer: {e}", file=sys.stderr)
                except Exception as cust_exc:
                    print(f"[Stripe Webhook] Customer creation error: {cust_exc}", file=sys.stderr)
        else:
            print(f"[Stripe Webhook] Received event type: {event_type} (not stored).", file=sys.stderr)
    except Exception as db_exc:
        print(f"[Stripe Webhook] DB error: {db_exc}", file=sys.stderr)
        raise HTTPException(status_code=500, detail=f"DB error: {db_exc}")

    print(f"[Stripe Webhook] Processing complete, returning success.", file=sys.stderr)
    return {"status": "success"}
