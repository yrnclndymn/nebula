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


# --- Exact-kill mutation tests (issue #163) ----------------------------------
# The suite above asserts the right inputs raise-or-pass; these pin the pieces a
# boundary/message mutant flips without any of the above noticing: the comparison
# edges (YEAR_MIN/MAX, headcount >= 0), the _coerce_int branches (whole-float
# coercion, per-branch messages), and the user-facing error-message contract the
# route surfaces at HTTP 422. Message asserts pin the distinguishing substring
# (or the exact contract string where a wrapper mutant would slip a substring).


def _err(field: object, value: object, source_url: object) -> str:
    """Return the FieldEditError message from validate_field_edit, or fail."""
    with pytest.raises(FieldEditError) as exc_info:
        validate_field_edit(field, value, source_url)
    return str(exc_info.value)


def test_source_url_that_breaks_urlparse_is_rejected():
    # A malformed IPv6 host makes urlparse raise ValueError; the except branch
    # must treat that as "not an http(s) URL" (return False), not accept it.
    assert "http(s)" in _err("yearFounded", 1998, "http://[::1")


def test_bool_rejection_message_names_the_field():
    # bool is an int subclass — its rejection message must name the field and say
    # what was wrong (pins both the bool-branch literal and the field argument).
    msg = _err("headcount", True, "https://example.com")
    assert "headcount" in msg
    assert "must be an integer" in msg


def test_whole_number_float_coerces_to_int():
    # An integral float is a valid headcount — the float-integrality branch must
    # convert it, not fall through or blow up.
    edit = validate_field_edit("headcount", 12.0, "https://example.com")
    assert edit.value == 12
    assert isinstance(edit.value, int)


def test_non_integer_float_message():
    assert "must be an integer" in _err("headcount", 12.5, "https://example.com")


def test_unparseable_string_message_names_the_field():
    msg = _err("headcount", "lots", "https://example.com")
    assert "headcount" in msg
    assert "must be an integer" in msg


def test_unsupported_value_type_message():
    # A value that is neither int/float/str/bool (here a list) hits the final
    # guard, which must still explain itself.
    assert "must be an integer" in _err("headcount", ["1200"], "https://example.com")


def test_unknown_field_message_lists_the_allowlist():
    assert "field must be one of" in _err("kind", "isv", "https://example.com")


def test_empty_source_url_is_treated_as_absent_for_optional_field():
    # "" (and whitespace) must normalise to None, not fall through to the
    # http(s)-scheme check — yearFounded doesn't require a source.
    assert validate_field_edit("yearFounded", 1998, "").source_url is None
    assert validate_field_edit("yearFounded", 1998, "   ").source_url is None


def test_bad_scheme_message_is_the_exact_contract_string():
    # Exact match (not substring): a wrapper/case mutant could hide inside a
    # substring, so pin the whole user-facing string.
    assert _err("funding", "Series A", "ftp://example.com") == ("source_url must be an http(s) URL")


def test_missing_required_source_message():
    assert "requires a source URL" in _err("headcount", 120, None)


def test_year_out_of_range_message_states_the_bounds():
    msg = _err("yearFounded", 1500, None)
    assert "between" in msg
    assert "1600" in msg
    assert "2100" in msg


def test_headcount_zero_is_allowed():
    # 0 is the non-negative boundary — a valid headcount, not an error.
    assert validate_field_edit("headcount", 0, "https://example.com").value == 0


def test_negative_headcount_message_is_the_exact_contract_string():
    assert _err("headcount", -5, "https://example.com") == (
        "headcount must be a non-negative integer"
    )


def test_blank_funding_text_message():
    assert "non-empty text" in _err("funding", "   ", "https://example.com")
