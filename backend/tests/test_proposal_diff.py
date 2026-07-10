"""Diffing a proposed enrichment against the existing record, and focus resolution."""

from app.agents.assistant.proposal_diff import (
    citation_matches_focus,
    compute_diff,
    resolve_focus,
)

_EMPTY_LEADERSHIP = {"added": [], "merged": [], "variants": []}


def test_resolve_focus_maps_aliases_to_record_keys():
    assert resolve_focus("headcount") == "headcount"
    assert resolve_focus("employees") == "headcount"
    assert resolve_focus("HQ") == "hq_location"
    assert resolve_focus("year founded") == "year_founded"
    assert resolve_focus("linkedin") == "linkedin"


def test_resolve_focus_none_for_unknown_or_empty():
    assert resolve_focus("") is None
    assert resolve_focus("clients") is None  # not a scalar we scope a commit to
    assert resolve_focus("something odd") is None


def test_compute_diff_classifies_new_changed_same():
    existing = {"headcount": 200, "hqLocation": "London", "clients": [], "partners": []}
    record = {
        "headcount": 250,  # changed
        "hq_location": "London",  # same
        "year_founded": 2015,  # new (absent from existing)
        "clients": [],
        "partnerships": [],
    }
    diff = compute_diff(existing, record, _EMPTY_LEADERSHIP)
    by_key = {s["key"]: s for s in diff["scalars"]}

    assert by_key["headcount"]["status"] == "changed"
    assert by_key["headcount"]["old"] == 200
    assert by_key["headcount"]["new"] == 250
    assert by_key["hq_location"]["status"] == "same"
    assert by_key["year_founded"]["status"] == "new"


def test_compute_diff_omits_empty_proposed_fields():
    existing = {"headcount": 200, "clients": [], "partners": []}
    record = {"headcount": 0, "about": "", "clients": [], "partnerships": []}
    diff = compute_diff(existing, record, _EMPTY_LEADERSHIP)
    assert diff["scalars"] == []  # nothing proposed → nothing to review


def test_compute_diff_lists_only_additions():
    existing = {"clients": ["Acme"], "partners": []}
    record = {"clients": ["Acme", "Globex"], "partnerships": ["Initech"]}
    diff = compute_diff(existing, record, _EMPTY_LEADERSHIP)
    assert diff["clients"]["added"] == ["Globex"]
    assert diff["clients"]["existing_count"] == 1
    assert diff["partners"]["added"] == ["Initech"]


def test_compute_diff_against_new_company():
    record = {"headcount": 40, "clients": [], "partnerships": []}
    diff = compute_diff(None, record, _EMPTY_LEADERSHIP)
    assert diff["scalars"][0]["status"] == "new"


def test_citation_matches_focus_by_alias():
    assert citation_matches_focus("headcount", "headcount")
    assert citation_matches_focus("employees", "headcount")
    assert citation_matches_focus("hq_location", "hq_location")
    assert not citation_matches_focus("funding", "headcount")
