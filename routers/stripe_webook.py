from fastapi import APIRouter, Request, HTTPException, Header
import stripe
import os
import sys
from pymongo import MongoClient
from bson import ObjectId

router = APIRouter(tags=["Payments"])

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

try:
    from routers.contract.contract_service import create_contract
    from routers.contract import contract_service as contract_svc
except Exception:
    try:
        from contract.contract_service import create_contract
        from contract import contract_service as contract_svc
    except Exception:
        create_contract = None
        contract_svc = None

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
        print(f"[Stripe Webhook] Event parsed successfully: {event.type}", file=sys.stderr)
    except ValueError as e:
        print(f"[Stripe Webhook] Invalid payload: {e}", file=sys.stderr)
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.SignatureVerificationError as e:
        print(f"[Stripe Webhook] Invalid Stripe signature: {e}", file=sys.stderr)
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except Exception as e:
        import traceback
        print(f"[Stripe Webhook] Unexpected error during event parsing: {e}\n{traceback.format_exc()}", file=sys.stderr)
        raise HTTPException(status_code=400, detail=f"Unexpected error: {e}")

    event_type = event.type
    data = event.data.object

    # stripe v5+ StripeObjects are not dicts — convert to plain dict for MongoDB
    import json as _json
    def _to_dict(obj):
        try:
            return _json.loads(str(obj))
        except Exception:
            return {}

    data_dict = _to_dict(data)
    event_dict = _to_dict(event)

    try:
        if event_type == "checkout.session.completed":
            record = {
                "event_type": event_type,
                "session": data_dict,
                "received_at": event.created,
                "stripe_event_id": event.id,
                "raw_event": event_dict,
            }
            result = stripe_completed_collection.insert_one(record)
            print(f"[Stripe Webhook] Stored completed session in DB with _id: {result.inserted_id}", file=sys.stderr)
            # If we have customer details in the session, create or find the customer
            cust_details = getattr(data, "customer_details", None)
            if cust_details and get_or_create_customer:
                name = getattr(cust_details, "name", None)
                email = getattr(cust_details, "email", None)
                phone = getattr(cust_details, "phone", None) or ""

                # Create or get existing customer
                try:
                    customer_id, existing = get_or_create_customer(
                        customer_collection, name or "", phone, email or ""
                    )
                    print(f"[Stripe Webhook] Customer id: {customer_id} | existing: {existing}", file=sys.stderr)

                    # Build address object from session and store on customer document
                    address = getattr(cust_details, "address", None)
                    if address:
                        addr_obj = {
                            "line1": getattr(address, "line1", None),
                            "line2": getattr(address, "line2", None),
                            "city": getattr(address, "city", None),
                            "state": getattr(address, "state", None),
                            "postal_code": getattr(address, "postal_code", None),
                            "country": getattr(address, "country", None),
                        }
                        try:
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
                    # Issue contracts (one per basket item) in the Contracts
                    # collection (idempotent per item).
                    try:
                        contracts = create_contract(data, customer_id) if create_contract else []
                        for contract in contracts or []:
                            customer_collection.update_one(
                                {"_id": ObjectId(customer_id)},
                                {"$addToSet": {"contract_refs": {
                                    "contract_id": str(contract["_id"]),
                                    "reference": contract.get("reference"),
                                    "status": contract.get("status"),
                                }}},
                            )
                            print(f"[Stripe Webhook] Issued contract {contract.get('reference')} "
                                  f"({contract.get('status')}) for customer {customer_id}",
                                  file=sys.stderr)
                    except Exception as c_err:
                        print(f"[Stripe Webhook] Failed to issue contract: {c_err}", file=sys.stderr)
                    # After customer exists/updated, attempt to pair customer to basket if metadata present
                    try:
                        metadata = getattr(data, "metadata", None)
                        basket_id = getattr(metadata, "basket_id", None) if metadata else None
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
        elif event_type == "invoice.paid" and contract_svc:
            # Monthly cover: activate on first invoice, renew on each cycle.
            # A subscription can back many device contracts, so this affects all.
            try:
                affected = contract_svc.handle_invoice_paid(data, event.id)
                for c in affected or []:
                    print(f"[Stripe Webhook] invoice.paid -> contract {c.get('reference')} "
                          f"({c.get('status')})", file=sys.stderr)
            except Exception as inv_exc:
                print(f"[Stripe Webhook] handle_invoice_paid failed: {inv_exc}", file=sys.stderr)
        elif event_type == "charge.refunded" and contract_svc:
            try:
                c = contract_svc.handle_charge_refunded(data, event.id)
                if c:
                    print(f"[Stripe Webhook] charge.refunded -> contract {c.get('reference')} "
                          f"({c.get('status')})", file=sys.stderr)
            except Exception as ref_exc:
                print(f"[Stripe Webhook] handle_charge_refunded failed: {ref_exc}", file=sys.stderr)
        elif event_type == "customer.subscription.deleted" and contract_svc:
            try:
                c = contract_svc.handle_subscription_deleted(data)
                if c:
                    print(f"[Stripe Webhook] subscription.deleted -> contract "
                          f"{c.get('reference')} cancelled", file=sys.stderr)
            except Exception as sub_exc:
                print(f"[Stripe Webhook] handle_subscription_deleted failed: {sub_exc}", file=sys.stderr)
        else:
            print(f"[Stripe Webhook] Received event type: {event_type} (not stored).", file=sys.stderr)
    except Exception as db_exc:
        print(f"[Stripe Webhook] DB error: {db_exc}", file=sys.stderr)
        raise HTTPException(status_code=500, detail=f"DB error: {db_exc}")

    print(f"[Stripe Webhook] Processing complete, returning success.", file=sys.stderr)
    return {"status": "success"}
