"""Unit tests for the deterministic CSV mapping (no LLM / no DB)."""

from app.importer.csv_import import _int_or_none, _map_row, build_record, heuristic_extract
from app.importer.extract import ExtractedFields


def test_header_mapping_tolerates_sheet_variations():
    raw = {
        "Company": "Acme",
        "HQ / Location": "Berlin, DE",
        "Estimated revenues": "$10M",
        "Headcount": "~250 staff",
    }
    row = _map_row(raw)
    assert row["name"] == "Acme"
    assert row["hq_location"] == "Berlin, DE"
    assert row["estimated_revenue"] == "$10M"
    assert row["headcount"] == "~250 staff"


def test_headcount_parses_first_integer():
    assert _int_or_none("~250 staff") == 250
    assert _int_or_none("1,200") == 1200
    assert _int_or_none("unknown") is None


def test_build_record_no_llm_splits_lists():
    row = _map_row(
        {
            "Company": "Acme",
            "Website": "acme.com",
            "Partnerships": "SAP, Microsoft; AWS",
            "Notes": "founded 2015",
        }
    )
    rec = build_record(row, "SAP ecosystem", heuristic_extract(row))
    assert rec is not None
    assert rec.name == "Acme"
    assert rec.website == "acme.com"
    assert rec.partnerships == ["SAP", "Microsoft", "AWS"]
    assert rec.topics == ["SAP ecosystem"]
    # Without the LLM, notes stay raw and year isn't split out.
    assert rec.notes == "founded 2015"
    assert rec.year_founded is None


def test_build_record_skips_nameless_row():
    row = _map_row({"Company": "", "Website": "x.com"})
    assert build_record(row, None, ExtractedFields()) is None
