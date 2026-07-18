"""Pure validation for user-initiated inline field edits (issue #149).

No database here — these cover the allowlist, int coercion, plausible-year range,
http(s)-only source URL, and the provenance source-required rule. Fictional data
only (public repo).
"""

import pytest

from app.graph.field_edit import (
    EDITABLE_FIELDS,
    FieldEditError,
    validate_field_edit,
)


def test_allowlist_rejects_unknown_field():
    with pytest.raises(FieldEditError):
        validate_field_edit("kind", "isv", "https://example.com")
    with pytest.raises(FieldEditError):
        validate_field_edit("name", "Acme", "https://example.com")


def test_editable_fields_are_the_v1_three():
    assert set(EDITABLE_FIELDS) == {"headcount", "yearFounded", "funding"}


def test_headcount_requires_source():
    with pytest.raises(FieldEditError):
        validate_field_edit("headcount", 120, None)
    with pytest.raises(FieldEditError):
        validate_field_edit("headcount", 120, "   ")


def test_funding_requires_source():
    with pytest.raises(FieldEditError):
        validate_field_edit("funding", "Series B", None)


def test_year_founded_source_optional():
    edit = validate_field_edit("yearFounded", 1998, None)
    assert edit.field == "yearFounded"
    assert edit.value == 1998
    assert edit.source_url is None


def test_headcount_coerces_int_from_string_with_commas():
    edit = validate_field_edit("headcount", "1,200", "https://example.com/about")
    assert edit.value == 1200
    assert edit.source_url == "https://example.com/about"


def test_headcount_rejects_non_integer():
    with pytest.raises(FieldEditError):
        validate_field_edit("headcount", "lots", "https://example.com")
    with pytest.raises(FieldEditError):
        validate_field_edit("headcount", 12.5, "https://example.com")


def test_headcount_rejects_negative():
    with pytest.raises(FieldEditError):
        validate_field_edit("headcount", -5, "https://example.com")


def test_headcount_rejects_bool():
    # bool is an int subclass in Python — a True/False must not sneak through.
    with pytest.raises(FieldEditError):
        validate_field_edit("headcount", True, "https://example.com")


def test_year_founded_range_enforced():
    with pytest.raises(FieldEditError):
        validate_field_edit("yearFounded", 1500, None)
    with pytest.raises(FieldEditError):
        validate_field_edit("yearFounded", 2500, None)
    # boundaries inclusive
    assert validate_field_edit("yearFounded", 1600, None).value == 1600
    assert validate_field_edit("yearFounded", 2100, None).value == 2100


def test_year_founded_coerces_string():
    assert validate_field_edit("yearFounded", "1998", None).value == 1998


def test_funding_is_free_text_and_trimmed():
    edit = validate_field_edit("funding", "  Series C, $40M  ", "https://example.com/news")
    assert edit.value == "Series C, $40M"


def test_funding_rejects_blank_text():
    with pytest.raises(FieldEditError):
        validate_field_edit("funding", "   ", "https://example.com")


def test_source_url_must_be_http_scheme():
    for bad in ("ftp://example.com", "javascript:alert(1)", "example.com", "/relative"):
        with pytest.raises(FieldEditError):
            validate_field_edit("funding", "Series A", bad)


def test_source_url_accepts_http_and_https():
    assert validate_field_edit("funding", "Series A", "http://example.com").source_url == (
        "http://example.com"
    )
    assert validate_field_edit("funding", "Series A", "https://example.com/x").source_url == (
        "https://example.com/x"
    )


def test_source_url_trimmed():
    edit = validate_field_edit("headcount", 10, "  https://example.com/about  ")
    assert edit.source_url == "https://example.com/about"


def test_bad_source_url_rejected_even_when_optional():
    # yearFounded doesn't require a source, but a provided one must still be valid.
    with pytest.raises(FieldEditError):
        validate_field_edit("yearFounded", 1998, "not-a-url")
