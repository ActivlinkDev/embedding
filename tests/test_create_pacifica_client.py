"""create_pacifica_client: dry run by default, inserts once, never duplicates a client."""

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "create_pacifica_client",
    Path(__file__).resolve().parent.parent / "scripts" / "create_pacifica_client.py",
)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)


class FakeCollection:
    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    def find_one(self, flt, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in flt.items()):
                return d
        return None

    def insert_one(self, doc):
        doc["_id"] = f"id{len(self.docs)}"
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])


def test_dry_run_writes_nothing():
    col = FakeCollection()
    assert script.create(col, apply=False) == 0
    assert col.docs == []


def test_apply_inserts_the_client_with_its_styles():
    col = FakeCollection()
    assert script.create(col, apply=True) == 0
    assert len(col.docs) == 1
    doc = col.docs[0]
    assert doc["ClientKey"] == "PAC82627"
    assert doc["Client_ID"] == "PACIFICA"
    assert doc["Styles"]["ButtonBackground"] == "#03AC83"


def test_existing_client_key_is_not_overwritten_or_duplicated():
    existing = {"_id": "x", "ClientKey": "PAC82627", "Client_ID": "SOMEONE_ELSE"}
    col = FakeCollection([existing])
    assert script.create(col, apply=True) == 1
    assert col.docs == [existing]


def test_existing_client_id_blocks_a_second_key():
    col = FakeCollection([{"_id": "x", "ClientKey": "OTHER", "Client_ID": "PACIFICA"}])
    assert script.create(col, apply=True) == 1
    assert len(col.docs) == 1


def test_every_colour_token_is_a_hex_colour():
    for key, value in script.STYLES.items():
        if key == "Logo_URL":
            assert value.startswith("https://")
        else:
            assert re.fullmatch(r"#[0-9A-F]{6}", value), key
