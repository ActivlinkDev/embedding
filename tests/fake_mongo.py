"""A tiny in-memory stand-in for the pymongo collection calls the email modules make.

Supports equality (including dotted paths), $in, $gte, $lt, $lte, $exists, $ne, $regex, unique indexes and
$set / $inc / $push / $setOnInsert updates — enough to exercise claim/dedupe, OTP counting and
tenant-scoped lookups without a database.
"""
import copy
import re
from datetime import datetime

from bson import ObjectId
from pymongo.errors import DuplicateKeyError


def _get(doc, path):
    node = doc
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


_MISSING = object()


def _cmp(a, b):
    if isinstance(a, datetime) and isinstance(b, datetime):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return a, b


def _matches(doc, query):
    for key, cond in query.items():
        if key == "$or":
            if not any(_matches(doc, q) for q in cond):
                return False
            continue
        value = _get(doc, key)
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            for op, arg in cond.items():
                if op == "$in" and (value is _MISSING or value not in arg):
                    return False
                if op == "$exists" and (value is not _MISSING) != bool(arg):
                    return False
                if op == "$ne" and value is not _MISSING and value == arg:
                    return False
                if op == "$regex":
                    flags = re.IGNORECASE if "i" in cond.get("$options", "") else 0
                    if not isinstance(value, str) or not re.search(arg, value, flags):
                        return False
                if op in ("$gte", "$lt", "$lte"):
                    if value is _MISSING:
                        return False
                    a, b = _cmp(value, arg)
                    if op == "$gte" and not a >= b:
                        return False
                    if op == "$lt" and not a < b:
                        return False
                    if op == "$lte" and not a <= b:
                        return False
        elif value is _MISSING or value != cond:
            return False
    return True


def _set(doc, path, value):
    parts = path.split(".")
    node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class Result:
    def __init__(self, matched=0, modified=0, inserted_id=None):
        self.matched_count = matched
        self.modified_count = modified
        self.inserted_id = inserted_id


class FakeCollection:
    def __init__(self, docs=(), unique=()):
        self.docs = [copy.deepcopy(d) for d in docs]
        for d in self.docs:
            d.setdefault("_id", ObjectId())
        self.unique = tuple(unique)

    def create_index(self, *args, **kwargs):
        return "ok"

    def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", ObjectId())
        for field in self.unique:
            if any(_get(d, field) == _get(doc, field) for d in self.docs):
                raise DuplicateKeyError(f"duplicate {field}")
        self.docs.append(doc)
        return Result(inserted_id=doc["_id"])

    def find(self, query=None, projection=None, sort=None):
        return [copy.deepcopy(d) for d in self.docs if _matches(d, query or {})]

    def find_one(self, query=None, projection=None, sort=None):
        found = self.find(query)
        if sort:
            key, direction = sort[0]
            found.sort(key=lambda d: _get(d, key), reverse=direction < 0)
        return found[0] if found else None

    def count_documents(self, query, **kwargs):
        return len(self.find(query))

    def _apply(self, doc, update):
        for path, value in (update.get("$set") or {}).items():
            _set(doc, path, value)
        for path, value in (update.get("$push") or {}).items():
            current = _get(doc, path)
            _set(doc, path, ([] if current is _MISSING else current) + [value])
        for path, value in (update.get("$inc") or {}).items():
            current = _get(doc, path)
            _set(doc, path, (0 if current is _MISSING else current) + value)

    def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if _matches(doc, query):
                self._apply(doc, update)
                return Result(1, 1)
        if upsert:
            doc = {k: v for k, v in query.items() if not k.startswith("$")}
            for path, value in (update.get("$setOnInsert") or {}).items():
                _set(doc, path, value)
            self._apply(doc, update)
            self.insert_one(doc)
        return Result(0, 0)

    def update_many(self, query, update):
        n = 0
        for doc in self.docs:
            if _matches(doc, query):
                self._apply(doc, update)
                n += 1
        return Result(n, n)
