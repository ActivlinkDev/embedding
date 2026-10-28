"""Choosing offer-page copy for a device category."""

import asyncio

import pytest

from conftest import seed_taxonomy
from routers.cms import category_offer
from routers.cms.category_offer import locale_chain, select_offer

TAXONOMY = {
    "Washer Dryer": {"category": "Washer Dryer", "group": "Laundry", "sector": "Home Appliances"},
    "Kettle": {"category": "Kettle", "group": "Kitchen", "sector": "Small Domestic Appliances"},
    "Smartphone": {"category": "Smartphone", "group": "Mobile", "sector": "Technology"},
}


def entry(name, level, headline, client=None, examples=None):
    return {
        "taxonomy_name": name,
        "level": level,
        "client": client,
        "locale": None,
        "headline": headline,
        "intro": None,
        "examples_heading": None,
        "examples": examples if examples is not None else [
            {"title": "t", "description": "d", "cover_type": "breakdown", "icon": None}
        ],
    }


def cms(by_locale):
    """A stand-in for the CMS: entries per locale, recording which locales were read."""
    reads = []

    async def entries_for(code):
        reads.append(code)
        return by_locale.get(code, [])

    entries_for.reads = reads
    return entries_for


def pick(category, locale, client=None, by_locale=None):
    return asyncio.run(select_offer(category, locale, client, entries_for=cms(by_locale or {})))


@pytest.fixture(autouse=True)
def taxonomy(monkeypatch):
    seed_taxonomy(monkeypatch, TAXONOMY)


def test_category_entry_wins_over_group_and_sector():
    found = pick("Washer Dryer", "en-GB", by_locale={"en-GB": [
        entry("Home Appliances", "sector", "sector copy"),
        entry("Laundry", "group", "group copy"),
        entry("Washer Dryer", "category", "category copy"),
    ]})
    assert found["headline"] == "category copy"


def test_falls_back_to_group_then_sector():
    group = pick("Washer Dryer", "en-GB", by_locale={"en-GB": [
        entry("Home Appliances", "sector", "sector copy"),
        entry("Laundry", "group", "group copy"),
    ]})
    sector = pick("Washer Dryer", "en-GB", by_locale={"en-GB": [entry("Home Appliances", "sector", "sector copy")]})
    assert group["headline"] == "group copy"
    assert sector["headline"] == "sector copy"


def test_name_match_folds_case_and_punctuation():
    found = pick("washer-dryer", "en-GB", by_locale={"en-GB": [entry("Washer Dryer", "category", "category copy")]})
    assert found["headline"] == "category copy"


def test_level_must_match_the_entry():
    # A "Laundry" entry filed as a category must not stand in for the Laundry group.
    assert pick("Washer Dryer", "en-GB", by_locale={"en-GB": [entry("Laundry", "category", "x")]}) is None


def test_unknown_category_with_no_entry_returns_none():
    assert pick("Hovercraft", "en-GB", by_locale={"en-GB": [entry("Laundry", "group", "group copy")]}) is None


def test_unknown_category_still_matches_its_own_name():
    found = pick("Hovercraft", "en-GB", by_locale={"en-GB": [entry("Hovercraft", "category", "own copy")]})
    assert found["headline"] == "own copy"


def test_client_override_applies_only_to_its_client():
    entries = {"en-GB": [
        entry("Smartphone", "category", "generic"),
        entry("Smartphone", "category", "acme only", client="ACME"),
    ]}
    assert pick("Smartphone", "en-GB", client="acme", by_locale=entries)["headline"] == "acme only"
    assert pick("Smartphone", "en-GB", client="OTHER", by_locale=entries)["headline"] == "generic"
    assert pick("Smartphone", "en-GB", client=None, by_locale=entries)["headline"] == "generic"


def test_client_override_at_a_broader_level_does_not_beat_a_specific_generic_entry():
    found = pick("Smartphone", "en-GB", client="ACME", by_locale={"en-GB": [
        entry("Technology", "sector", "acme sector", client="ACME"),
        entry("Smartphone", "category", "generic category"),
    ]})
    assert found["headline"] == "generic category"


def test_fr_be_reads_french_from_fr_fr():
    found = pick("Kettle", "fr-BE", by_locale={
        "fr-FR": [entry("Kettle", "category", "bouilloire")],
        "en-GB": [entry("Kettle", "category", "kettle")],
    })
    assert found["headline"] == "bouilloire"
    assert found["locale"] == "fr-FR"


def test_nl_be_falls_back_to_nl_nl_then_en_gb():
    dutch = pick("Kettle", "nl-BE", by_locale={
        "nl-NL": [entry("Kettle", "category", "waterkoker")],
        "en-GB": [entry("Kettle", "category", "kettle")],
    })
    english = pick("Kettle", "nl-BE", by_locale={"en-GB": [entry("Kettle", "category", "kettle")]})
    assert dutch["headline"] == "waterkoker"
    assert english["headline"] == "kettle"


def test_es_es_falls_back_to_en_gb():
    found = pick("Kettle", "es-ES", by_locale={"en-GB": [entry("Kettle", "category", "kettle")]})
    assert found["headline"] == "kettle"


def test_specific_english_beats_generic_in_the_requested_language():
    found = pick("Kettle", "es-ES", by_locale={
        "es-ES": [entry("Small Domestic Appliances", "sector", "genérico")],
        "en-GB": [entry("Kettle", "category", "kettle")],
    })
    assert found["headline"] == "kettle"


def test_empty_entries_are_skipped():
    found = pick("Kettle", "en-GB", by_locale={"en-GB": [
        entry("Kettle", "category", None, examples=[]),
        entry("Kitchen", "group", "kitchen"),
    ]})
    assert found["headline"] == "kitchen"


def test_locale_chain_order():
    chain = locale_chain("fr-BE")
    assert chain[0] == "fr-BE"
    assert chain[-1] == "en-GB"
    assert chain.index("fr-FR") < chain.index("en-GB")
    assert locale_chain("en-GB") == ["en-GB", "en-IE"]


def test_unknown_cover_type_is_treated_as_accidental():
    normalised = category_offer._normalise({
        "Taxonomy_Name": "Kettle",
        "Level": "category",
        "Headline": "h",
        "Damage_Examples": [
            {"Title": "a", "Cover_Type": "breakdown"},
            {"Title": "b", "Cover_Type": "something-new"},
            {"Title": "", "Cover_Type": "breakdown"},
        ],
    })
    assert [e["cover_type"] for e in normalised["examples"]] == ["breakdown", "accidental"]


def test_normalise_rejects_unknown_levels():
    assert category_offer._normalise({"Taxonomy_Name": "Kettle", "Level": "aisle"}) is None


def test_cms_failure_serves_the_previous_cache(monkeypatch):
    monkeypatch.setattr(category_offer, "_cache", {"en-GB": (0.0, [entry("Kettle", "category", "stale")])})

    async def boom(code):
        raise RuntimeError("CMS down")

    monkeypatch.setattr(category_offer, "_fetch_locale", boom)
    assert asyncio.run(category_offer._entries_for_locale("en-GB"))[0]["headline"] == "stale"
    assert asyncio.run(category_offer._entries_for_locale("es-ES")) == []
