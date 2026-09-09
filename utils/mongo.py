"""Process-wide MongoDB client.

Every module used to build its own ``MongoClient`` at import time. With a ``mongodb+srv://``
URI each construction performs an SRV and a TXT DNS lookup, so N modules meant N lookups —
plus N connection pools and N topology monitors against the same cluster.

This shares a single client, created lazily with ``connect=False``. Importing a module now
does no I/O at all; the DNS resolution and handshake happen once, on the first real query.

``get_db()`` and ``get_collection()`` return ordinary pymongo handles, which are themselves
free to create, so module-level ``db["SomeCollection"]`` stays cheap.
"""
import os
import threading
from typing import Optional

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

DEFAULT_DB_NAME = "Activlink"

_client: Optional[MongoClient] = None
_client_uri: Optional[str] = None
_lock = threading.Lock()


def get_client() -> Optional[MongoClient]:
    """Return the shared client, or None when MONGO_URI is unset.

    Callers that treat a missing database as fatal should use ``require_client``; the ones
    that degrade gracefully (portal, jwt_auth) rely on the None.
    """
    global _client, _client_uri

    uri = os.getenv("MONGO_URI")
    if not uri:
        return None

    # Re-read the URI so a test that repoints MONGO_URI gets a matching client rather than
    # a stale one bound to the previous value.
    if _client is not None and uri == _client_uri:
        return _client

    with _lock:
        if _client is not None and uri == _client_uri:
            return _client
        if _client is not None:
            _client.close()
        # connect=False keeps SRV resolution out of import time. It also means the client is
        # created after any fork, which is what pymongo wants.
        _client = MongoClient(uri, connect=False)
        _client_uri = uri
        return _client


def require_client() -> MongoClient:
    """Return the shared client, raising if MONGO_URI is unset."""
    client = get_client()
    if client is None:
        raise RuntimeError("MONGO_URI is not set; a MongoDB connection is required here.")
    return client


def get_db(name: str = DEFAULT_DB_NAME) -> Optional[Database]:
    """Return a database handle on the shared client, or None when MONGO_URI is unset."""
    client = get_client()
    return client[name] if client is not None else None


def require_db(name: str = DEFAULT_DB_NAME) -> Database:
    """Return a database handle, raising if MONGO_URI is unset."""
    return require_client()[name]


def get_collection(name: str, db_name: str = DEFAULT_DB_NAME) -> Optional[Collection]:
    """Return a collection handle, or None when MONGO_URI is unset."""
    db = get_db(db_name)
    return db[name] if db is not None else None


def reset_client() -> None:
    """Drop the shared client. For tests, and for anything that re-points MONGO_URI."""
    global _client, _client_uri
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
        _client_uri = None
