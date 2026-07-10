"""Heuristic people reconciliation — the Andy/Andrew de-duplication."""

from app.agents.assistant.reconcile import match_person, reconcile_people


def test_match_nickname_same_surname():
    assert match_person("Andy Smith", "Andrew Smith") == "same"
    assert match_person("Bob Jones", "Robert Jones") == "same"
    assert match_person("Kate Brown", "Katherine Brown") == "same"


def test_match_requires_same_surname():
    # Same first name, different surname → different people.
    assert match_person("Andrew Smith", "Andrew Baker") == "no"
    # Nickname pair but different surname → not a match.
    assert match_person("Andy Smith", "Andrew Baker") == "no"


def test_match_initial_is_uncertain():
    assert match_person("A. Smith", "Andrew Smith") == "maybe"
    assert match_person("Andrew Smith", "A Smith") == "maybe"


def test_match_different_first_names_same_surname():
    # Alan and Andrew share an initial but are not nickname-linked → not merged.
    assert match_person("Alan Smith", "Andrew Smith") == "no"


def test_reconcile_merges_confident_variant_onto_existing():
    existing = [{"name": "Andrew Smith", "title": "CEO"}]
    proposed = [{"name": "Andy Smith", "title": "Chief Executive"}]
    out = reconcile_people(existing, proposed)

    # Written under the EXISTING canonical name (no duplicate node created).
    assert out["reconciled"] == [{"name": "Andrew Smith", "title": "Chief Executive"}]
    assert out["merged"] == [
        {"proposed": "Andy Smith", "canonical": "Andrew Smith", "title": "Chief Executive"}
    ]
    assert out["added"] == []
    assert out["variants"] == []


def test_reconcile_flags_uncertain_and_does_not_write_it():
    existing = [{"name": "Andrew Smith", "title": "CEO"}]
    proposed = [{"name": "A. Smith", "title": "Founder"}]
    out = reconcile_people(existing, proposed)

    # Uncertain variant is surfaced but NOT written (avoids a probable duplicate).
    assert out["reconciled"] == []
    assert out["variants"] == [{"name": "A. Smith", "title": "Founder", "possibly": "Andrew Smith"}]


def test_reconcile_keeps_genuinely_new_people():
    existing = [{"name": "Andrew Smith", "title": "CEO"}]
    proposed = [
        {"name": "Andrew Smith", "title": "CEO"},
        {"name": "Priya Patel", "title": "CTO"},
    ]
    out = reconcile_people(existing, proposed)

    assert {"name": "Priya Patel", "title": "CTO"} in out["reconciled"]
    assert out["added"] == [{"name": "Priya Patel", "title": "CTO"}]


def test_reconcile_dedupes_within_proposed():
    # No existing record; the proposal itself lists the same person twice.
    out = reconcile_people([], [{"name": "Andrew Smith"}, {"name": "Andy Smith"}])
    names = [leader["name"] for leader in out["reconciled"]]
    assert names == ["Andrew Smith"]  # the second variant collapses onto the first
    assert out["merged"][0]["proposed"] == "Andy Smith"
