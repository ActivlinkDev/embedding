"""CLI to create a ServiceClient credential (frontend, internal script, or partner/MCP caller).

Usage:
    python scripts/issue_service_client.py --client-id frontend --scopes internal:* \\
        [--client-key AOPON12345] [--created-by "your-name"]

Prints the raw client_secret once — only its hash is stored, so save it now.

The same operation is available over HTTP as POST /auth/clients (see the Auth section of
/docs), which needs an 'admin:clients' scope. Use this CLI to bootstrap the first admin
client, or whenever you have Mongo access but not an admin token.
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.jwt_auth import create_service_client, generate_client_secret  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Issue a new ServiceClient credential")
    parser.add_argument("--client-id", required=True, help="Unique identifier for the caller, e.g. 'frontend' or 'AO-manufacturer'")
    parser.add_argument("--scopes", required=True, nargs="+", help="Space-separated scopes, e.g. sku:read device:write")
    parser.add_argument("--client-key", default=None, help="ClientKey.ClientKey this caller is scoped to, if any")
    parser.add_argument("--created-by", default=None, help="Who is issuing this credential")
    args = parser.parse_args()

    client_secret = generate_client_secret()

    try:
        create_service_client(
            client_id=args.client_id,
            client_secret=client_secret,
            scopes=args.scopes,
            client_key=args.client_key,
            created_by=args.created_by,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print("ServiceClient created. Save this secret now — it cannot be retrieved again:")
    print(f"  client_id:     {args.client_id}")
    print(f"  client_secret: {client_secret}")
    print(f"  scopes:        {' '.join(args.scopes)}")
    if args.client_key:
        print(f"  client_key:    {args.client_key}")


if __name__ == "__main__":
    main()
