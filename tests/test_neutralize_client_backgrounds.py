"""neutralize_client_backgrounds: plans per-field writes, backs up, applies and restores."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "neutralize_client_backgrounds",
    Path(__file__).resolve().parent.parent / "scripts" / "neutralize_client_backgrounds.py",
)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

NEUTRAL = script.NEUTRAL_SURFACES


class FakeCollection:
    """Just enough of a pymongo collection: find, find_one and update_one with dotted $set."""

    def __init__(self, docs):
        self.docs = [json.loads(json.dumps(d)) for d in docs]
        self.updates = []

    def _match(self, doc, flt):
        for key, cond in flt.items():
            if isinstance(cond, dict) and "$in" in cond:
                if doc.get(key) not in cond["$in"]:
                    return False
            elif doc.get(key) != cond:
                return False
        return True

    def find(self, flt, projection=None):
        return [json.loads(json.dumps(d)) for d in self.docs if self._match(d, flt)]

    def find_one(self, flt, projection=None):
        found = self.find(flt)
        return found[0] if found else None

    def update_one(self, flt, update):
        self.updates.append((flt, update))
        for doc in self.docs:
            if self._match(doc, flt):
                for path, value in update.get("$set", {}).items():
                    target = doc
                    parts = path.split(".")
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = value
                for path in update.get("$unset", {}):
                    doc.pop(path, None)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)


def _args(**kw):
    base = dict(apply=False, client_key=[], only_existing=False, backup_dir="backups", restore=None, db="Activlink")
    base.update(kw)
    return SimpleNamespace(**base)


AO = {
    "_id": "a1", "ClientKey": "AO12345",
    "Styles": {"Accent": "#7AB800", "ButtonText": "#FFFFFF", "Logo_URL": "x.png",
               "SecondaryBackground": "#F3F8EC", "CardBackground": "#F3F8EC", "Border": "#DDEBC8"},
}
NEUTRAL_CLIENT = {"_id": "n1", "ClientKey": "NEUTRAL", "Styles": dict(NEUTRAL)}


def test_only_surface_tokens_are_planned_and_brand_tokens_are_left_alone():
    fields = {field for field, _, _ in script.plan_client(AO)}
    assert fields == set(NEUTRAL)
    assert not fields & {"Accent", "ButtonText", "Logo_URL"}


def test_each_field_is_its_own_dotted_set_never_the_whole_styles_object():
    update = script.update_for(script.plan_client(AO))
    assert set(update) == {"$set"}
    assert "Styles" not in update["$set"]
    assert all(path.startswith("Styles.") for path in update["$set"])


def test_only_existing_skips_tokens_the_client_never_set():
    fields = {field for field, _, _ in script.plan_client(AO, only_existing=True)}
    assert fields == {"SecondaryBackground", "CardBackground", "Border"}


def test_already_neutral_values_are_skipped_whatever_their_case_or_spacing():
    doc = {"Styles": {k: f" {v.lower()} " for k, v in NEUTRAL.items()}}
    assert script.plan_client(doc) == []


def test_missing_styles_gets_the_full_set_and_non_object_styles_is_skipped():
    assert len(script.plan_client({"ClientKey": "X"})) == len(NEUTRAL)
    assert script.plan_client({"ClientKey": "X", "Styles": "oops"}) == []


def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    col = FakeCollection([AO, NEUTRAL_CLIENT])
    monkeypatch.setattr(script, "_connect", lambda db: col)
    assert script.run_neutralize(_args(backup_dir=str(tmp_path))) == 0
    assert col.updates == []
    assert list(tmp_path.iterdir()) == []


def test_apply_backs_up_first_then_updates_only_the_selected_client(monkeypatch, tmp_path):
    other = {"_id": "o1", "ClientKey": "OTHER", "Styles": {"CardBackground": "#000000"}}
    col = FakeCollection([AO, other])
    monkeypatch.setattr(script, "_connect", lambda db: col)
    assert script.run_neutralize(_args(apply=True, client_key=["AO12345"], backup_dir=str(tmp_path))) == 0

    backups = list(tmp_path.glob("client_styles_*.json"))
    assert len(backups) == 1
    saved = json.loads(backups[0].read_text())["clients"]
    assert saved == [{"_id": "a1", "ClientKey": "AO12345", "Styles": AO["Styles"]}]

    ao_now = col.find_one({"ClientKey": "AO12345"})["Styles"]
    assert all(ao_now[k] == v for k, v in NEUTRAL.items())
    assert ao_now["Accent"] == "#7AB800" and ao_now["Logo_URL"] == "x.png"
    assert col.find_one({"ClientKey": "OTHER"})["Styles"] == {"CardBackground": "#000000"}
    # Matched on both _id and ClientKey.
    assert all(set(flt) == {"_id", "ClientKey"} for flt, _ in col.updates)


def test_restore_puts_back_the_backed_up_styles(monkeypatch, tmp_path):
    col = FakeCollection([AO])
    monkeypatch.setattr(script, "_connect", lambda db: col)
    script.run_neutralize(_args(apply=True, backup_dir=str(tmp_path)))
    backup = next(tmp_path.glob("client_styles_*.json"))

    bson = pytest.importorskip("bson")
    # The fake stores _id as the plain string from the fixture, so let ObjectId pass it through.
    monkeypatch.setattr(bson, "ObjectId", lambda value: value)
    assert script.run_restore(_args(apply=True, restore=str(backup))) == 0
    assert col.find_one({"ClientKey": "AO12345"})["Styles"] == AO["Styles"]


def test_restore_reports_later_edits_outside_the_neutral_fields():
    assert script.untouched_differences({"Accent": "#111", "Border": "#a"}, {"Accent": "#222", "Border": "#b"}) == ["Accent"]
