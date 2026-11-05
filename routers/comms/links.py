"""Deep links in confirmation emails.

A registration email links to ``/my-registrations?link=<token>`` and a plan email to
``/my-contracts?link=<token>``. The token **grants nothing**: those pages still require the
customer to verify by SMS or email code. It only lets the page

- address the code to the email the link was sent to, without the customer typing it, and
- highlight the linked device or contract, once the verified customer's own results contain it.

The token is 192 random bits (``secrets.token_urlsafe(24)``); only its SHA-256 is stored, and a
link is bound to the tenant (``client_id``) that sent it, so on another client's storefront it
resolves to nothing.
"""
from __future__ import annotations

import secrets
from typing import Optional

from routers.comms import store

LINK_KINDS = ("device", "contract")
LINK_PAGES = {"device": "my-registrations", "contract": "my-contracts"}


def create_link(kind: str, ref_id: str, client_id: str, client_key: str,
                email: Optional[str], locale: Optional[str]) -> str:
    """Store a new link and return its token. The token is never stored or logged."""
    if kind not in LINK_KINDS:
        raise ValueError(f"Unknown link kind {kind!r}")
    token = secrets.token_urlsafe(24)
    store.links.insert_one({
        "token_hash": store.token_hash(token),
        "kind": kind,
        "ref_id": ref_id,
        "client_id": client_id,
        "client_key": client_key,
        "email": email,
        "locale": locale,
        "revoked": False,
        "created_at": store.now(),
    })
    return token


def link_url(base: str, kind: str, token: str, locale: Optional[str]) -> str:
    url = f"{base}/{LINK_PAGES[kind]}?link={token}"
    return f"{url}&locale={locale}" if locale else url


def resolve_link(token: Optional[str], client_id: Optional[str]) -> Optional[dict]:
    """The link for ``token`` if it exists, is live and belongs to ``client_id``.

    Unknown, revoked and other-tenant tokens are indistinguishable to the caller."""
    if not client_id or not isinstance(token, str) or not (16 <= len(token) <= 128):
        return None
    link = store.links.find_one({"token_hash": store.token_hash(token)})
    if not link or link.get("revoked") or link.get("client_id") != client_id:
        return None
    return link


def highlight_id(token: Optional[str], client_id: Optional[str], kind: str, ids: set) -> Optional[str]:
    """The record a link points at, but only when it is among ``ids`` — the records the
    verified customer is already being shown. Anything else returns None, so a link can never
    widen what a verification reveals."""
    link = resolve_link(token, client_id)
    if not link or link.get("kind") != kind:
        return None
    ref = str(link.get("ref_id") or "")
    return ref if ref in ids else None
