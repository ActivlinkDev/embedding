"""Test fixtures.

These tests exercise the matching and factor logic as pure functions, so they
import the modules with MONGO_URI unset — the module-level MongoClient is lazy
and no test touches a database.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A parseable URI that is never connected to: pymongo is lazy, and these tests
# patch out every read. Without one, module-level MongoClient() fails at import.
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017/test")
os.environ.setdefault("API_TOKEN", "test-token")


def seed_taxonomy(monkeypatch, taxonomy):
    """Install a category taxonomy, keyed exactly as ``_load`` keys it.

    Fixtures must not hand-build the cache: the lookup key is folded, so a dict
    keyed by display name would silently never match.
    """
    import time
    from utils import category_tree

    folded = {category_tree._key(name): dict(entry) for name, entry in taxonomy.items()}
    monkeypatch.setattr(category_tree, "_load", lambda: dict(folded))
    monkeypatch.setattr(category_tree, "_cache", dict(folded), raising=False)
    monkeypatch.setattr(category_tree, "_cache_loaded_at", time.monotonic(), raising=False)
