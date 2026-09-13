"""Portal type-ahead over the MasterSKU catalogue."""

from bson import ObjectId
from fastapi import HTTPException
import pytest

from routers.sku import master_search


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs
        self.sorted_with = None

    def sort(self, spec):
        self.sorted_with = spec
        return self

    def __iter__(self):
        return iter(self.docs)


class FakeCollection:
    """Records what was asked for; returns what the test seeded."""

    def __init__(self, docs=None):
        self.docs = docs or []
        self.pipeline = None
        self.find_filter = None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return iter(self.docs)

    def find(self, query, projection=None):
        self.find_filter = query
        return FakeCursor(self.docs)


def master(**overrides):
    doc = {
        "_id": ObjectId(),
        "identifiers": {
            "make": "Bosch",
            "makeNormalized": "bosch",
            "model": "SMS6ZCI00G",
            "modelNormalized": "sms6zci00g",
            "gtins": ["5012345678900"],
        },
        "category": "Dishwasher",
        "imageUrl": "https://example.test/product.png",
        "localeTitles": [{"k": "en_GB", "title": "Bosch dishwasher"}],
    }
    doc.update(overrides)
    return doc


def call(monkeypatch, *, masters=None, customs=None, client=None, **kwargs):
    master_collection = FakeCollection(masters if masters is not None else [])
    custom_collection = FakeCollection(customs or [])
    monkeypatch.setattr(master_search, "master_collection", master_collection)
    monkeypatch.setattr(master_search, "custom_collection", custom_collection)
    monkeypatch.setattr(master_search.catalog, "client_for_key", lambda _key: client)
    params = {"q": "bos", "field": "any", "clientKey": None, "locale": None, "limit": 10}
    params.update(kwargs)
    response = master_search.master_search(**params, _=None)
    return response, master_collection, custom_collection


def test_short_query_is_rejected_before_touching_mongo(monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        call(monkeypatch, q="b")
    assert excinfo.value.status_code == 400


def test_make_search_matches_a_normalized_prefix(monkeypatch):
    _, masters, _ = call(monkeypatch, q="  BoS ", field="make", masters=[master()])

    match = masters.pipeline[0]["$match"]
    # Anchored and folded: an unanchored regex would scan the whole catalogue,
    # and the stored value is normalized lower case.
    assert match == {"identifiers.makeNormalized": {"$regex": "^bos", "$options": "i"}}


def test_regex_metacharacters_in_the_query_are_escaped(monkeypatch):
    _, masters, _ = call(monkeypatch, q="a.*b", field="model")

    assert masters.pipeline[0]["$match"] == {
        "identifiers.modelNormalized": {"$regex": "^a\\.\\*b", "$options": "i"}
    }


def test_suggestion_carries_the_identifiers_the_form_fills_in(monkeypatch):
    response, _, _ = call(monkeypatch, masters=[master()], locale="en_GB")

    row = response["results"][0]
    assert row["make"] == "Bosch"
    assert row["model"] == "SMS6ZCI00G"
    assert row["gtins"] == ["5012345678900"]
    assert row["category"] == "Dishwasher"
    assert row["title"] == "Bosch dishwasher"
    # The portal only sends masterSkuId for a locale the master already has.
    assert row["locales"] == ["en_GB"]
    assert response["count"] == 1


def test_title_falls_back_to_another_locale_then_to_the_identifiers(monkeypatch):
    response, _, _ = call(
        monkeypatch,
        masters=[master(), master(localeTitles=[])],
        locale="fr_FR",
    )

    # Enriched for en_GB only: still recognisable when creating for fr_FR.
    assert response["results"][0]["title"] == "Bosch dishwasher"
    assert response["results"][1]["title"] == "Bosch SMS6ZCI00G"


def test_existing_membership_is_scoped_to_the_callers_own_client(monkeypatch):
    doc = master()
    mine = {
        "_id": ObjectId(),
        "masterSkuId": doc["_id"],
        "sku": "AO-123",
        "enabledLocales": ["en_GB"],
    }
    response, _, customs = call(
        monkeypatch,
        masters=[doc],
        customs=[mine],
        client={"Client_ID": "client-1"},
        clientKey="key-1",
    )

    assert customs.find_filter["clientId"] == "client-1"
    assert customs.find_filter["masterSkuId"] == {"$in": [doc["_id"]]}
    assert response["results"][0]["existing"]["sku"] == "AO-123"


def test_no_client_key_reports_no_membership_at_all(monkeypatch):
    response, _, customs = call(monkeypatch, masters=[master()])

    # Without a client there is nobody to report membership for, and the
    # CustomSKU collection — the tenant-scoped one — is never read.
    assert "existing" not in response["results"][0]
    assert customs.find_filter is None


def test_unknown_client_key_is_rejected(monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        call(monkeypatch, client=None, clientKey="nope")
    assert excinfo.value.status_code == 404
