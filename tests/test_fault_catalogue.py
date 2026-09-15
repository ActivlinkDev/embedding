from unittest.mock import MagicMock

import pytest

from routers.faults import catalogue as route
from routers.faults.fault_types import FAULT_TYPES, TYPE_ORDER, normalise_type_id


def fault(type_id="no_power", issue="Will not start", fault_id="no_power-01"):
    return {"typeId": type_id, "faultId": fault_id, "Issue": issue, "Description": "d", "Solution": "s"}


# --- the fixed vocabulary ---------------------------------------------------------------

@pytest.mark.parametrize("supplied,expected", [
    ("no_power", "no_power"),
    ("No Power", "no_power"),
    ("leak-water", "leak_water"),
    ("catastrophic_gremlins", "other"),
    ("", "other"),
    (None, "other"),
    (17, "other"),
], ids=["exact", "cased", "hyphenated", "invented", "empty", "none", "not-a-string"])
def test_generated_type_is_coerced_into_the_vocabulary(supplied, expected):
    # A type the model invents must never reach storage: it would group under a label no
    # other category shares, and nothing downstream could report on it.
    assert normalise_type_id(supplied) == expected


def test_grouping_follows_the_fixed_order_whatever_order_faults_arrive_in():
    grouped = route.group_faults([
        fault("other", "Something odd"),
        fault("physical_damage", "Dropped it"),
        fault("no_power", "Dead"),
    ])
    assert [group["typeId"] for group in grouped] == ["no_power", "physical_damage", "other"]
    assert [TYPE_ORDER.index(group["typeId"]) for group in grouped] == sorted(
        TYPE_ORDER.index(group["typeId"]) for group in grouped)


def test_types_with_no_faults_are_omitted_rather_than_returned_empty():
    # A kettle has no connectivity faults; rendering an empty card would be a dead end.
    grouped = route.group_faults([fault("no_power")])
    assert [group["typeId"] for group in grouped] == ["no_power"]
    assert all(group["faults"] for group in grouped)


def test_localized_label_wins_over_the_english_fallback():
    grouped = route.group_faults([fault("no_power")], {"no_power": "Ne démarre pas"})
    assert grouped[0]["typeLabel"] == "Ne démarre pas"


def test_missing_localized_label_falls_back_to_english_rather_than_blank():
    grouped = route.group_faults([fault("no_power")], {})
    assert grouped[0]["typeLabel"] == FAULT_TYPES["no_power"]


def test_fault_ids_are_unique_and_namespaced_by_type():
    assigned = route._assign_fault_ids([
        {"typeId": "no_power"}, {"typeId": "no_power"}, {"typeId": "leak_water"}, {"typeId": "bogus"}])
    ids = [entry["faultId"] for entry in assigned]
    assert ids == ["no_power-01", "no_power-02", "leak_water-01", "other-01"]
    assert len(set(ids)) == len(ids)


# --- serving and generating -------------------------------------------------------------

def test_stored_catalogue_is_served_without_calling_the_model(monkeypatch):
    stored = [{"typeId": "no_power", "typeLabel": "Won't turn on", "faults": [fault()]}]
    db = MagicMock()
    db.find_one.return_value = {"Category": "Dishwasher", "Content": [
        {"locale": "en_GB", "FaultTypes": stored, "source": "llm"}]}
    monkeypatch.setattr(route, "catalogue_collection", db)
    monkeypatch.setattr(route, "generate_catalogue", lambda *a, **k: pytest.fail("must not generate"))

    fault_types, message = route.load_catalogue("Dishwasher", "en_GB")
    assert fault_types == stored
    assert "already stored" in message


def test_generation_failure_returns_an_empty_catalogue_not_an_error(monkeypatch):
    # The customer can still describe the fault in their own words. An OpenAI outage must
    # not stop a repair booking, so this path returns empty rather than raising.
    db = MagicMock()
    db.find_one.return_value = None
    monkeypatch.setattr(route, "catalogue_collection", db)

    def explode(*_args, **_kwargs):
        raise RuntimeError("openai down")

    monkeypatch.setattr(route, "generate_catalogue", explode)

    fault_types, message = route.load_catalogue("Dishwasher", "en_GB")
    assert fault_types == []
    assert "own words" in message
    db.update_one.assert_not_called()


def test_unknown_category_is_generated_rather_than_404(monkeypatch):
    db = MagicMock()
    db.find_one.return_value = None
    monkeypatch.setattr(route, "catalogue_collection", db)
    monkeypatch.setattr(route, "generate_catalogue", lambda *a, **k: ([fault()], {}))
    monkeypatch.setattr(route, "store_catalogue", lambda *a, **k: None)

    fault_types, _ = route.load_catalogue("Nothing We Know", "en_GB")
    assert [group["typeId"] for group in fault_types] == ["no_power"]


def test_regenerating_a_locale_replaces_its_entry_instead_of_appending(monkeypatch):
    # Two Content entries for one locale would make find_locale_content return whichever
    # happened to sort first — so the old entry is pulled before the new one is pushed.
    db = MagicMock()
    monkeypatch.setattr(route, "catalogue_collection", db)
    route.store_catalogue("Dishwasher", "en_GB", [fault()], {})

    pull_update = db.update_one.call_args_list[0].args[1]
    push_update = db.update_one.call_args_list[1].args[1]
    assert pull_update["$pull"] == {"Content": {"locale": "en_GB"}}
    assert push_update["$push"]["Content"]["locale"] == "en_GB"
    assert push_update["$push"]["Content"]["source"] == route.SOURCE_LLM


def test_model_reply_is_parsed_out_of_a_fenced_block():
    assert route._parse_model_json('```json\n{"faults": []}\n```') == {"faults": []}
    assert route._parse_model_json('{"faults": []}') == {"faults": []}


def test_generated_faults_without_an_issue_are_dropped(monkeypatch):
    reply = MagicMock()
    reply.choices = [MagicMock(message=MagicMock(content=
        '{"faults": [{"typeId": "no_power", "Issue": "", "Description": "d", "Solution": "s"},'
        ' {"typeId": "no_power", "Issue": "Dead", "Description": "d", "Solution": "s"}]}'))]
    client = MagicMock()
    client.chat.completions.create.return_value = reply
    monkeypatch.setattr(route, "_openai", lambda: client)

    faults, _ = route.generate_catalogue("Dishwasher", "en_GB")
    assert [entry["Issue"] for entry in faults] == ["Dead"]


def test_generation_raises_when_the_model_returns_nothing_usable(monkeypatch):
    reply = MagicMock()
    reply.choices = [MagicMock(message=MagicMock(content='{"faults": []}'))]
    client = MagicMock()
    client.chat.completions.create.return_value = reply
    monkeypatch.setattr(route, "_openai", lambda: client)

    with pytest.raises(ValueError):
        route.generate_catalogue("Dishwasher", "en_GB")
